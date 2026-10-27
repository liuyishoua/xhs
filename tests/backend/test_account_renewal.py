"""Authenticated rotations, stale writers, and logout are distinct outcomes."""
import asyncio
import json
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


@pytest.mark.parametrize('kind', ['pc', 'creator'])
def test_disabled_accounts_are_not_checked_or_reactivated(database, monkeypatch, kind):
    from backend.app.services import scheduler_service as scheduler
    from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
    from backend.app.adapters.xhs.creator_login_adapter import XhsCreatorLoginAdapter
    from backend.app.core.time import shanghai_now
    def forbidden(*args, **kwargs):
        pytest.fail('Stopped accounts must never contact the platform')
    monkeypatch.setattr(XhsPcLoginAdapter, 'get_user_info', forbidden)
    monkeypatch.setattr(XhsCreatorLoginAdapter, 'check_session', forbidden)
    monkeypatch.setattr(scheduler, 'SessionLocal', database)
    with database() as db:
        account = db.get(PlatformAccount, 1)
        account.sub_type = kind
        account.status = 'disabled'
        account.status_message = '主人停用'
        db.commit()
        assert scheduler._check_single_account(db, account, shanghai_now()) == 'disabled'
    scheduler.check_all_account_cookies_once()
    with database() as db:
        account = db.get(PlatformAccount, 1)
        assert account.status == 'disabled' and account.status_message == '主人停用'


def test_scheduled_check_persists_rotation_and_records_only_safe_metadata(database, monkeypatch):
    from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
    from backend.app.services.scheduler_service import _check_single_account
    from backend.app.core.time import shanghai_now
    from backend.app.models import ApiLog
    def check(self, cookies):
        cookies['web_session'] = 'rotated-secret-value'
        return {'external_user_id': 'own'}
    monkeypatch.setattr(XhsPcLoginAdapter, 'get_user_info', check)
    with database() as db:
        account = db.get(PlatformAccount, 1)
        _check_single_account(db, account, shanghai_now())
        db.commit()
        assert account.status == 'active'
    with database() as db:
        row = db.scalars(select(AccountCookieVersion)).one()
        assert decode_cookie_text(decrypt_text(row.encrypted_cookies))['web_session'] == 'rotated-secret-value'
        events = list(db.scalars(select(ApiLog)))
        assert {e.status for e in events} == {'refreshed', 'active'}
        assert events[0].meta['changed_cookie_names'] == ['web_session']
        assert all('rotated-secret-value' not in str(e.meta) for e in events)


@pytest.mark.parametrize('failure,status', [
    (TimeoutError('private-transport-detail'), 'unknown'),
    (profile_failure({'code': -100}), 'expired'),
    (profile_failure({'code': 300012}), 'risk'),
])
def test_scheduled_check_does_not_turn_transport_errors_into_expiry(database, monkeypatch, failure, status):
    from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
    from backend.app.services.scheduler_service import _check_single_account
    from backend.app.core.time import shanghai_now
    def check(self, cookies):
        cookies['web_session'] = 'anonymous'
        raise failure
    monkeypatch.setattr(XhsPcLoginAdapter, 'get_user_info', check)
    with database() as db:
        account = db.get(PlatformAccount, 1)
        _check_single_account(db, account, shanghai_now())
        db.commit()
        assert account.status == status
        assert 'private-transport-detail' not in account.status_message
        row = db.scalars(select(AccountCookieVersion)).one()
        assert decode_cookie_text(decrypt_text(row.encrypted_cookies))['web_session'] == 'old'


@pytest.mark.parametrize('new_version', [False, True])
@pytest.mark.parametrize('path', ['scheduled', 'manual'])
def test_check_failure_cannot_expire_a_concurrent_successful_login(database, monkeypatch, new_version, path):
    from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
    from backend.app.services.scheduler_service import _check_single_account
    from backend.app.api.accounts import check_account
    from backend.app.core.time import shanghai_now
    from fastapi import HTTPException
    def check(cookies):
        with database() as other:
            if new_version:
                other.add(AccountCookieVersion(platform_account_id=1, encrypted_cookies=encrypt_text('a1=device; web_session=new-login')))
            else:
                assert persist_refreshed_pc_cookies(other, 1, {'a1': 'device', 'web_session': 'old'}, {'a1': 'device', 'web_session': 'new-login'})
            other.get(PlatformAccount, 1).status = 'active'
            other.commit()
        raise profile_failure({'code': -100})
    monkeypatch.setattr(XhsPcLoginAdapter, 'get_user_info', lambda self, cookies: check(cookies))
    with database() as db:
        if path == 'scheduled':
            _check_single_account(db, db.get(PlatformAccount, 1), shanghai_now())
            db.commit()
        else:
            with pytest.raises(HTTPException) as error:
                check_account(1, current_user=SimpleNamespace(id=1), db=db, pc_adapter=SimpleNamespace(get_user_info=check), creator_adapter=None, self_profile_adapter=None)
            assert error.value.status_code == 409
    with database() as db:
        assert db.get(PlatformAccount, 1).status == 'active'
        latest = db.scalar(select(AccountCookieVersion).order_by(AccountCookieVersion.id.desc()))
        assert decode_cookie_text(decrypt_text(latest.encrypted_cookies))['web_session'] == 'new-login'


def test_comment_identity_check_persists_verified_cookie_rotation(database, monkeypatch, tmp_path):
    from backend.app.services import content_automation as mod
    monkeypatch.setattr(mod, 'SessionLocal', database)
    executor = mod.Executor({'user_id': 1, 'pc_account_id': 1, 'expected_external_user_id': 'own'}, mod.Ledger(tmp_path / 'ledger.sqlite3'), object(), object())
    def client(identity):
        return SimpleNamespace(cookies='a1=device; web_session=old', refreshed_cookies={'a1': 'device', 'web_session': 'rotated'},
                               get_self_info=lambda: {'user_id': identity, 'guest': False})
    executor.pc = lambda: client('wrong-account')
    with pytest.raises(ValueError):
        executor.verify_identity()
    with database() as db:
        assert decode_cookie_text(decrypt_text(db.scalars(select(AccountCookieVersion)).one().encrypted_cookies))['web_session'] == 'old'
    executor.pc = lambda: client('own')
    executor.verify_identity()
    with database() as db:
        assert decode_cookie_text(decrypt_text(db.scalars(select(AccountCookieVersion)).one().encrypted_cookies))['web_session'] == 'rotated'


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
        def recent_peers(self, **kwargs): return []
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


@pytest.mark.parametrize('login_changed', [False, True])
def test_listener_ignores_concurrent_request_cookie_rotation_but_rejects_new_login(database, monkeypatch, login_changed):
    from backend.app.services import dm_worker
    monkeypatch.setattr(dm_worker, 'SessionLocal', database)
    original={'a1':'device','web_session':'old','acw_tc':'initial'}
    current={**original,'acw_tc':'comment-request'}
    if login_changed:current['web_session']='new-login'
    with database() as db:
        row=db.scalars(select(AccountCookieVersion)).one()
        row.encrypted_cookies=encrypt_text(json.dumps(current));db.commit()
    if login_changed:
        with pytest.raises(ConnectionError):
            dm_worker.save_connection_cookies(1,original,{**original,'acw_tc':'im-request'})
    else:
        dm_worker.save_connection_cookies(1,original,{**original,'acw_tc':'im-request'})
    with database() as db:
        assert decode_cookie_text(decrypt_text(db.scalars(select(AccountCookieVersion)).one().encrypted_cookies))==current


def test_recovery_reads_live_socket_and_dispatches_before_unrelated_backlog(database, monkeypatch):
    from backend.app.services import dm_worker
    import threading
    monkeypatch.setattr(dm_worker, 'SessionLocal', database)
    monkeypatch.setattr(dm_worker.xhs_network, 'require_ready', lambda: None)
    monkeypatch.setattr(dm_worker, 'save_connection_cookies', lambda *a: None)
    monkeypatch.setattr(dm_worker, 'reconnect_peers', lambda *a: ['active-peer', 'old-peer'])
    reading = threading.Event()
    recovered = []
    class Connection:
        def __init__(self, *args): pass
        async def connect(self): pass
        def cookie_snapshot(self): return {'a1': 'device', 'web_session': 'old'}
        def recent_peers(self, **kwargs): return ['old-peer', 'active-peer']
        def history_page(self, *args): pass
        async def events(self):
            reading.set()
            await asyncio.Event().wait()
            yield {}
        async def heartbeat(self): pass
        async def close(self): pass
    def recover(*args, only_peer, **kwargs):
        assert reading.wait(2), 'socket must be consumed during history recovery'
        recovered.append(only_peer)
        return 0
    def dispatch(*args, blocked_peers, **kwargs):
        assert recovered == ['active-peer']
        assert blocked_peers == {'old-peer'}
        raise asyncio.CancelledError()
    monkeypatch.setattr(dm_worker, 'XhsImConnection', Connection)
    monkeypatch.setattr(dm_worker, 'reconnect_history', recover)
    monkeypatch.setattr(dm_worker, 'next_delivery', dispatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dm_worker.account_loop(1))


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


@pytest.mark.parametrize('path', ['scheduled', 'manual'])
@pytest.mark.parametrize('result', ['rotated', 'risk', 'wrong_identity'])
def test_creator_checks_upload_capability_and_protects_identity(database, monkeypatch, path, result):
    from backend.app.adapters.xhs.creator_login_adapter import XhsCreatorLoginAdapter
    from backend.app.services.scheduler_service import _check_single_account
    from backend.app.api.accounts import check_account
    from backend.app.services.account_health import creator_session_failure
    from backend.app.core.time import shanghai_now
    with database() as db:
        db.get(PlatformAccount, 1).sub_type = 'creator'
        db.commit()
    calls = []
    def check(self, cookies):
        calls.append(True)
        cookies['web_session'] = 'rotated'
        if result == 'risk':
            raise creator_session_failure(906, 401)
        return {'external_user_id': 'other' if result == 'wrong_identity' else 'own'}
    monkeypatch.setattr(XhsCreatorLoginAdapter, 'check_session', check)
    with database() as db:
        if path == 'scheduled':
            _check_single_account(db, db.get(PlatformAccount, 1), shanghai_now())
            db.commit()
        else:
            check_account(1, current_user=SimpleNamespace(id=1), db=db, pc_adapter=None,
                          creator_adapter=XhsCreatorLoginAdapter(), self_profile_adapter=None)
    with database() as db:
        account = db.get(PlatformAccount, 1)
        assert account.status == ('active' if result == 'rotated' else 'risk')
        assert account.external_user_id == 'own'
        cookie = db.scalars(select(AccountCookieVersion)).one()
        assert decode_cookie_text(decrypt_text(cookie.encrypted_cookies))['web_session'] == ('rotated' if result == 'rotated' else 'old')
    assert calls == [True]


@pytest.mark.parametrize('success', [True, False])
def test_creator_profile_rotation_only_exposed_after_success(monkeypatch, success):
    from apis import xhs_creator_login_apis
    from backend.app.adapters.xhs.creator_login_adapter import XhsCreatorLoginAdapter
    closed = []
    class Api:
        last_user_info_code = -100 if not success else 0
        last_user_info_http_status = 200
        def __init__(self, **kwargs): pass
        def get_user_info(self, cookies):
            return success, {'userId': 'own'}, {'a1': 'device', 'web_session': 'rotated'}
        def close(self): closed.append(True)
    monkeypatch.setattr(xhs_creator_login_apis, 'XHSCreatorLoginApi', Api)
    cookies = {'a1': 'device', 'web_session': 'old'}
    if success:
        assert XhsCreatorLoginAdapter().get_user_info(cookies)['external_user_id'] == 'own'
        assert cookies['web_session'] == 'rotated'
    else:
        with pytest.raises(AccountCheckError):
            XhsCreatorLoginAdapter().get_user_info(cookies)
        assert cookies['web_session'] == 'old'
    assert closed == [True]


@pytest.mark.parametrize('prefix', ['Cookie: ', 'cookie:', 'COOKIE: '])
def test_saved_cookie_header_prefix_is_not_a_cookie_name(prefix):
    from backend.app.services.account_service import cookie_header_from_text
    text = prefix + 'gid=edge; a1=device; customer-sso-sid=token:with:colon'
    assert decode_cookie_text(text) == {'gid': 'edge', 'a1': 'device', 'customer-sso-sid': 'token:with:colon'}
    assert cookie_header_from_text(text) == 'gid=edge; a1=device; customer-sso-sid=token:with:colon'


@pytest.mark.parametrize('state', ['expired_sso_active', 'expired_sso_inactive', 'risk', 'expired_upload', 'risk_upload'])
def test_creator_auto_renew_uses_existing_sso_only_and_never_retries_risk(monkeypatch, state):
    from contextlib import contextmanager
    from backend.app.adapters.xhs.creator_login_adapter import XhsCreatorLoginAdapter
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter
    from backend.app.services.account_health import creator_session_failure, CreatorUploadError
    adapter = XhsCreatorLoginAdapter()
    profiles, exchanges, permits = [], [], []
    def profile(cookies):
        profiles.append(dict(cookies))
        if cookies['token'] == 'old' and not state.endswith('_upload'):
            raise creator_session_failure(906 if state == 'risk' else -100, 401)
        return {'external_user_id': 'own'}
    def exchange(cookies):
        exchanges.append(dict(cookies))
        return {'active': state in ('expired_sso_active', 'expired_upload'), 'cookies': {'a1': 'same-device', 'token': 'new'}}
    @contextmanager
    def api():
        yield SimpleNamespace(query_session=exchange)
    monkeypatch.setattr(adapter, '_api', api)
    monkeypatch.setattr(adapter, 'get_user_info', profile)
    def permit(self):
        permits.append(True)
        if state.endswith('_upload') and 'token=old' in self.cookies:
            raise CreatorUploadError({'http_status': 401, 'business_code': 906 if state == 'risk_upload' else -100})
        self.refreshed_cookies = {'token': 'new'}
    monkeypatch.setattr(XhsCreatorApiAdapter, 'check_upload_permission', permit)
    cookies = {'a1': 'same-device', 'token': 'old'}
    if state in ('expired_sso_active', 'expired_upload'):
        assert adapter.check_session(cookies)['external_user_id'] == 'own'
        assert cookies == {'a1': 'same-device', 'token': 'new'}
        assert len(profiles) == 2 and len(permits) == (2 if state == 'expired_upload' else 1)
    else:
        with pytest.raises(AccountCheckError):
            adapter.check_session(cookies)
        assert cookies['token'] == 'old'
        assert len(profiles) == 1 and len(permits) == (1 if state == 'risk_upload' else 0)
    assert len(exchanges) == (0 if state in ('risk', 'risk_upload') else 1)
