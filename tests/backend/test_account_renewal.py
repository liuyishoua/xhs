"""Authenticated rotations, stale writers, and logout are distinct outcomes."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base
from backend.app.core.security import decrypt_text, encrypt_text
from backend.app.models import AccountCookieVersion, PlatformAccount, User
from backend.app.models.direct_message import DmListener
from backend.app.services.account_health import AccountCheckError, profile_failure
from backend.app.services.account_service import decode_cookie_text, persist_refreshed_pc_cookies


@pytest.fixture
def database(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'renewal.db'))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(User(id=1, username='owner', password_hash='test'))
        db.add(PlatformAccount(id=1, user_id=1, platform='xhs', sub_type='pc', external_user_id='own'))
        db.add(AccountCookieVersion(platform_account_id=1, encrypted_cookies=encrypt_text('a1=device; web_session=old')))
        db.add(DmListener(account_id=1, enabled=True, auto_reply=False))
        db.commit()
    yield factory
    engine.dispose()


def test_rotations_are_encrypted_bounded_and_survive_new_session(database):
    before = {'a1': 'device', 'web_session': 'old'}
    after = {**before, 'web_session': 'rotated'}
    with database() as db:
        assert persist_refreshed_pc_cookies(db, 1, before, after)
        db.commit()
    with database() as db:
        row = db.scalars(select(AccountCookieVersion)).one()
        assert 'rotated' not in row.encrypted_cookies
        assert decode_cookie_text(decrypt_text(row.encrypted_cookies)) == after
        assert persist_refreshed_pc_cookies(db, 1, after, after)


@pytest.mark.parametrize('new_login', [False, True])
def test_stale_rotation_cannot_replace_newer_rotation_or_qr_login(database, new_login):
    before = {'a1': 'device', 'web_session': 'old'}
    with database() as db:
        if new_login:
            db.add(AccountCookieVersion(platform_account_id=1, encrypted_cookies=encrypt_text('a1=device; web_session=new-login')))
        else:
            assert persist_refreshed_pc_cookies(db, 1, before, {**before, 'web_session': 'new-login'})
        db.commit()
    with database() as db:
        assert not persist_refreshed_pc_cookies(db, 1, before, {**before, 'web_session': 'stale'})
        latest = db.scalar(select(AccountCookieVersion).order_by(AccountCookieVersion.id.desc()))
        assert decode_cookie_text(decrypt_text(latest.encrypted_cookies))['web_session'] == 'new-login'


@pytest.mark.parametrize('code,status', [(-100, 'expired'), (300012, 'risk'), (None, 'unknown'), (-999, 'unknown')])
def test_only_explicit_logout_is_expired(code, status):
    error = profile_failure({'code': code, 'msg': 'private-response'})
    assert error.account_status == status
    assert 'private-response' not in str(error)


@pytest.mark.parametrize('success', [True, False])
def test_im_http_only_merges_cookies_from_accepted_responses(monkeypatch, success):
    from backend.app.adapters.xhs import im_adapter
    updates = []
    response = SimpleNamespace(cookies={'web_session': 'replacement'}, json=lambda: {'success': success})
    connection = object.__new__(im_adapter.XhsImConnection)
    connection.auth = SimpleNamespace(
        cookies='a1=device; web_session=old', user_id='own', sign_cookie='', dsl_pair='', proxies=None,
        next_sign_context=lambda api: {'now': 1, 'tier': 'test'}, current_b1=lambda now: '',
        origin=lambda kind: 'https://edith.xiaohongshu.com', cookies_for_url=lambda target, cookies: cookies,
        http_client=SimpleNamespace(get=lambda *args, **kwargs: response),
        update_cookies=lambda cookies, **kwargs: updates.append(cookies))
    monkeypatch.setattr(im_adapter, 'generate_request_params', lambda *a, **kw: ({}, {}, ''))
    monkeypatch.setattr(im_adapter, 'build_pc_business_headers', lambda headers, *a, **kw: headers)
    connection._signed_get('/api/im/web/v3/chats')
    assert updates == ([{'web_session': 'replacement'}] if success else [])


def test_pc_login_adapter_preserves_failure_code_and_closes_client(monkeypatch):
    from apis import xhs_pc_login_apis
    from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
    closed = []
    class Api:
        last_user_info_code = -100
        def __init__(self, **kwargs): pass
        def get_user_info(self, cookies):
            cookies['web_session'] = 'anonymous'
            return False, {}, cookies
        def close(self): closed.append(True)
    monkeypatch.setattr(xhs_pc_login_apis, 'XHSLoginApi', Api)
    with pytest.raises(AccountCheckError) as caught:
        XhsPcLoginAdapter().get_user_info({'web_session': 'old'})
    assert caught.value.account_status == 'expired'
    assert closed == [True]


@pytest.mark.parametrize('error,status', [
    (TimeoutError('private-transport-data'), 'unknown'),
    (AccountCheckError('expired', '登录已失效'), 'expired'),
    (AccountCheckError('risk', '需要验证'), 'risk'),
])
def test_failed_account_check_keeps_original_credentials(database, error, status):
    from backend.app.api.accounts import check_account
    def get_user_info(cookies):
        cookies['web_session'] = 'anonymous'
        raise error
    with database() as db:
        result = check_account(1, current_user=SimpleNamespace(id=1), db=db,
            pc_adapter=SimpleNamespace(get_user_info=get_user_info), creator_adapter=None, self_profile_adapter=None)
        assert result['status'] == status
        assert 'private-transport-data' not in result['status_message']
    with database() as db:
        row = db.scalars(select(AccountCookieVersion)).one()
        assert decode_cookie_text(decrypt_text(row.encrypted_cookies))['web_session'] == 'old'


def test_verified_check_persists_rotated_cookie(database):
    from backend.app.api.accounts import check_account
    def get_user_info(cookies):
        cookies['web_session'] = 'rotated'
        return {'external_user_id': 'own', 'nickname': 'test'}
    with database() as db:
        result = check_account(1, current_user=SimpleNamespace(id=1), db=db,
            pc_adapter=SimpleNamespace(get_user_info=get_user_info), creator_adapter=None, self_profile_adapter=None)
        assert result['status'] == 'active'
    with database() as db:
        row = db.scalars(select(AccountCookieVersion)).one()
        assert decode_cookie_text(decrypt_text(row.encrypted_cookies))['web_session'] == 'rotated'


def test_listener_periodically_revalidates_login(database, monkeypatch):
    from backend.app.services import dm_worker
    monkeypatch.setattr(dm_worker, 'SessionLocal', database)
    monkeypatch.setattr(dm_worker.xhs_network, 'require_ready', lambda: None)
    monkeypatch.setattr(dm_worker, 'reconnect_history', lambda *args: 0)
    ticks = iter([0, 0, 601])
    monkeypatch.setattr(dm_worker, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    refreshed = []
    class Connection:
        def __init__(self, *args): pass
        async def connect(self): pass
        def cookie_snapshot(self): return {'a1': 'device', 'web_session': 'rotated'}
        def recent_peers(self): return []
        def history_page(self, *args): pass
        def refresh_login(self):
            refreshed.append(True)
            raise asyncio.CancelledError()
        async def events(self):
            await asyncio.Event().wait()
            yield {}
        async def heartbeat(self): pass
        async def close(self): pass
    monkeypatch.setattr(dm_worker, 'XhsImConnection', Connection)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dm_worker.account_loop(1))
    assert refreshed == [True]
    with database() as db:
        row = db.scalars(select(AccountCookieVersion)).one()
        assert decode_cookie_text(decrypt_text(row.encrypted_cookies))['web_session'] == 'rotated'


def test_expired_listener_waits_for_new_credentials_then_reconnects(database, monkeypatch):
    from backend.app.services import dm_worker
    monkeypatch.setattr(dm_worker, 'SessionLocal', database)
    monkeypatch.setattr(dm_worker.xhs_network, 'require_ready', lambda: None)
    attempts = []
    class Connection:
        def __init__(self, cookies, user_id): attempts.append(cookies)
        async def connect(self): raise AccountCheckError('expired', '登录已失效')
        async def close(self): pass
    monkeypatch.setattr(dm_worker, 'XhsImConnection', Connection)
    original_sleep = asyncio.sleep
    async def exercise():
        waiting, resume, retried = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def sleep(delay):
            if delay == 30:
                waiting.set()
                await resume.wait()
                if len(attempts) >= 2:
                    retried.set()
                    await asyncio.Event().wait()
            else:
                await original_sleep(0)
        monkeypatch.setattr(dm_worker, 'asyncio', SimpleNamespace(
            to_thread=asyncio.to_thread, CancelledError=asyncio.CancelledError,
            gather=asyncio.gather, sleep=sleep))
        task = asyncio.create_task(dm_worker.account_loop(1))
        try:
            await asyncio.wait_for(waiting.wait(), 3)
            assert len(attempts) == 1
            with database() as db:
                assert db.get(PlatformAccount, 1).status == 'expired'
                row = db.scalars(select(AccountCookieVersion)).one()
                assert decode_cookie_text(decrypt_text(row.encrypted_cookies))['web_session'] == 'old'
                db.add(AccountCookieVersion(platform_account_id=1, encrypted_cookies=encrypt_text('a1=device; web_session=new-login')))
                db.commit()
            resume.set()
            await asyncio.wait_for(retried.wait(), 3)
            assert len(attempts) == 2 and 'new-login' in attempts[1]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())
