"""Authentication loss during discovery and sending resumes only unsent work."""
from types import SimpleNamespace
import asyncio

import pytest
from sqlalchemy import select

from test_proactive_interactions import setup, Platform, start, configure, state
from test_proactive_discovery import discovery
from backend.app.core.security import encrypt_text, decrypt_text
from backend.app.models import AccountCookieVersion, ApiLog, PlatformAccount
from backend.app.services import proactive_interactions as service
from backend.app.services import dm_worker
from backend.app.models.direct_message import DmManualMessage
from backend.app.services.account_health import AccountCheckError, profile_failure


def credential(sessions, value='synthetic-session'):
    with sessions() as db:
        db.add(AccountCookieVersion(platform_account_id=1, encrypted_cookies=encrypt_text(value)))
        db.commit()


def restore(sessions):
    with sessions() as db:
        db.get(PlatformAccount, 1).status = 'active'
        db.commit()


@pytest.mark.parametrize('source', ['recommend', 'visitors', 'outreach'])
def test_already_expired_scheduled_account_is_visible_and_reports_once(discovery, source):
    client, sessions, _ = discovery
    credential(sessions)
    configure(client, source=source, keyword='露营', enabled=True)
    with sessions() as db:
        db.get(PlatformAccount, 1).status = 'expired'
        db.commit()
    with service.store() as ledger:
        ledger.execute("UPDATE strategy_settings SET next_at='2000-01-01'")
    service.tick(); service.tick()
    assert state(client)['runs'][0]['state'] == 'waiting_login'
    with sessions() as db:
        events = db.scalars(select(ApiLog)).all()
        assert len(events) == 1 and events[0].meta['source'] == 'comment_identity_check'
    assert not Platform.writes
    restore(sessions)
    service.tick()
    assert state(client)['runs'][0]['state'] == ('waiting_delivery' if source == 'outreach' else 'completed')


@pytest.mark.parametrize('stage', ['search', 'detail', 'preview', 'mentions', 'likes', 'recent'])
def test_read_failure_submits_recovery_instead_of_being_swallowed(discovery, monkeypatch, stage):
    client, sessions, _ = discovery
    credential(sessions)
    method = {'search':'search_note', 'detail':'get_note_info', 'preview':'get_comment_preview',
              'mentions':'get_mentions', 'likes':'get_likes', 'recent':'get_recent_user_notes'}[stage]
    original = getattr(Platform, method)
    def expired(*args, **kwargs):
        if stage in ('search', 'detail'):
            return False, 'private response', {'success': False, 'code': -100}
        raise profile_failure({'code': -100})
    monkeypatch.setattr(Platform, method, expired)
    if stage == 'recent':
        monkeypatch.setattr(Platform, 'get_recent_user_notes', expired)
    source = 'visitors' if stage in ('mentions', 'likes', 'recent') else 'recommend'
    start(client, source=source, keyword='露营')
    service.tick()
    result = state(client)
    assert result['runs'][0]['state'] == 'waiting_login'
    with sessions() as db:
        assert db.get(PlatformAccount, 1).status == 'expired'
        assert db.scalars(select(ApiLog)).one().meta['business_code'] == -100
    assert not Platform.writes
    monkeypatch.setattr(Platform, method, original)
    restore(sessions)
    service.tick()
    assert state(client)['runs'][0]['state'] == 'completed'


def test_send_expiry_preserves_text_and_does_not_repeat_prior_comment(discovery, monkeypatch):
    client, sessions, _ = discovery
    credential(sessions)
    attempts = []
    expired = [True]
    original = Platform.post_comment
    def post(self, note, text):
        attempts.append((note, text))
        if note == 'n1' and expired[0]:
            raise profile_failure({'code': -100})
        return original(self, note, text)
    monkeypatch.setattr(Platform, 'post_comment', post)
    start(client, keyword='露营', browse_limit=3)
    service.tick()
    result = state(client)
    assert result['runs'][0]['state'] == 'waiting_login'
    assert sorted(i['state'] for i in result['items']) == ['ready', 'submitted']
    assert [note for note, _ in Platform.writes] == ['n0']
    service.tick()
    assert len(attempts) == 2
    restore(sessions); expired[0] = False
    service.tick(); service.tick()
    assert [note for note, _ in Platform.writes] == ['n0', 'n1']
    assert attempts[1] == attempts[2]  # Same generated text, never resend n0.
    assert state(client)['runs'][0]['state'] == 'completed'


def test_stale_read_error_cannot_expire_a_replaced_credential(discovery, monkeypatch):
    client, sessions, _ = discovery
    credential(sessions, 'old')
    def expired(*args, **kwargs):
        credential(sessions, 'new')
        raise profile_failure({'code': -100})
    monkeypatch.setattr(Platform, 'search_note', expired)
    start(client, keyword='露营'); service.tick()
    with sessions() as db:
        assert db.get(PlatformAccount, 1).status == 'active'
        assert db.scalars(select(ApiLog)).first() is None
    assert not Platform.writes


def test_send_failure_after_cookie_rotation_keeps_recovery_state(discovery, monkeypatch):
    client, sessions, _ = discovery
    credential(sessions, '{"web_session":"old"}')
    rotation = [0]
    def init(self, cookies):
        self.cookies = cookies
        self.refreshed_cookies = None
    def profile(self):
        rotation[0] += 1
        self.refreshed_cookies = {'web_session':str(rotation[0])}
        return {'user_id':'self'}
    def cookies(db, user, account_id):
        version = db.scalar(select(AccountCookieVersion).order_by(AccountCookieVersion.id.desc()))
        return decrypt_text(version.encrypted_cookies)
    def reject(*args):
        raise profile_failure({'code': -100})
    monkeypatch.setattr(Platform, '__init__', init)
    monkeypatch.setattr(Platform, 'get_self_info', profile)
    monkeypatch.setattr(Platform, 'post_comment', reject)
    monkeypatch.setattr(service, '_get_owned_pc_account_cookies', cookies)
    start(client, keyword='露营'); service.tick()
    assert rotation[0] >= 2
    assert state(client)['runs'][0]['state'] == 'waiting_login'
    assert state(client)['items'][0]['state'] == 'ready'
    with sessions() as db:
        assert db.get(PlatformAccount, 1).status == 'expired'
        assert len(db.scalars(select(ApiLog).where(ApiLog.status=='expired')).all()) == 1


def test_refreshed_session_is_usable_by_real_pc_cookie_parser(setup, monkeypatch):
    from backend.app.adapters.xhs.pc_api_adapter import XhsPcApiAdapter
    from backend.app.services.account_service import cookie_header_from_text, decode_cookie_text
    from xhs_utils.xhs_pc import XHSPcAuth
    client, sessions = setup
    credential(sessions, '{"a1":"synthetic-device","web_session":"old"}')
    parsed = []
    def api(self):
        auth = XHSPcAuth.from_cookie(self.cookies)
        parsed.append(auth.profile.cookie_map)
        def profile():
            auth.profile.update_cookies({'web_session':'rotated'})
            return True, '', {'data':{'user_id':'self'}}
        return SimpleNamespace(auth=auth, get_user_me=profile, http=SimpleNamespace(close=auth.http_client.close),
                               search_note=lambda **kwargs: (True, '', {'data':{'items':[]}}))
    def cookies(db, user, account_id):
        version = db.scalar(select(AccountCookieVersion).order_by(AccountCookieVersion.id.desc()))
        return cookie_header_from_text(decrypt_text(version.encrypted_cookies))
    monkeypatch.setattr(service, 'XhsPcApiAdapter', XhsPcApiAdapter)
    monkeypatch.setattr(service, '_get_owned_pc_account_cookies', cookies)
    monkeypatch.setattr(XhsPcApiAdapter, '_api', api)
    monkeypatch.setattr(service.xhs_network, 'http_options', lambda: {})
    with sessions() as db:
        _, adapter = service.adapter_for(db, 1, 1, 'self')
        assert adapter.search_note('露营')[0]
        assert parsed[-1]['a1'] == 'synthetic-device'
        assert parsed[-1]['web_session'] == 'rotated'
        version = db.scalar(select(AccountCookieVersion))
        assert decode_cookie_text(decrypt_text(version.encrypted_cookies))['web_session'] == 'rotated'


@pytest.mark.parametrize('after_share', [False, True])
def test_private_auth_rejection_resumes_pair_without_resending_share(discovery, after_share):
    from test_proactive_discovery import deliver
    client, sessions, _ = discovery
    credential(sessions)
    start(client, source='outreach', keyword='露营', share_note=True); service.tick()
    if after_share:
        assert deliver(sessions)[0]
    pending = {}
    class Socket:
        async def send(self, peer, text, mid):
            pending[mid].set_result({'mid':mid, 'code':-100, 'msg':'login expired'})
    with pytest.raises(AccountCheckError) as exc:
        asyncio.run(dm_worker.deliver_manual(1, Socket(), pending, 'self'))
    # account_loop catches the propagated failure against its connection cookies.
    dm_worker.account_failure(1, 'synthetic-session', exc.value)
    service.tick()
    assert state(client)['runs'][0]['state'] == 'waiting_delivery'
    with sessions() as db:
        assert db.get(PlatformAccount, 1).status == 'expired'
        messages = db.scalars(select(DmManualMessage).order_by(DmManualMessage.id)).all()
        assert [m.state for m in messages] == (['sent','queued'] if after_share else ['queued','queued'])
    assert deliver(sessions) == (False, [])
    restore(sessions)
    writes = []
    while True:
        sent, batch = deliver(sessions)
        writes.extend(batch)
        if not sent:break
    service.tick()
    assert len(writes) == (1 if after_share else 2)
    assert state(client)['runs'][0]['state'] == 'completed'


def test_transient_read_failure_does_not_request_login(discovery, monkeypatch):
    client, sessions, _ = discovery
    credential(sessions)
    monkeypatch.setattr(Platform, 'search_note', lambda *a, **kw: (False, '', {'code': -1}))
    start(client, keyword='露营'); service.tick()
    assert state(client)['runs'][0]['state'] == 'failed'
    with sessions() as db:
        assert db.get(PlatformAccount, 1).status == 'active'
        assert db.scalars(select(ApiLog)).first() is None


@pytest.mark.parametrize('code,status', [(-100, 'expired'), (300012, 'risk')])
def test_post_transport_exposes_explicit_authentication_failure(monkeypatch, code, status):
    from backend.app.adapters.xhs.pc_api_adapter import XhsPcApiAdapter
    closed = []
    api = SimpleNamespace(base_url='https://example.invalid',
        http=SimpleNamespace(post=lambda *a, **kw: SimpleNamespace(status_code=200, headers={},
             json=lambda: {'success':False, 'code':code}), close=lambda:closed.append(True)),
        _request_params=lambda *a: ({}, {}, '{}'))
    monkeypatch.setattr(XhsPcApiAdapter, '_api', lambda self: api)
    with pytest.raises(AccountCheckError) as exc:
        XhsPcApiAdapter('synthetic').post_comment('n', 'hello')
    assert exc.value.account_status == status and closed == [True]
