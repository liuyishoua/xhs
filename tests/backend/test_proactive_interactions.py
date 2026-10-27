import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.api import proactive_interactions as api
from backend.app.core.database import Base, get_db
from backend.app.core.security import create_access_token
from backend.app.main import create_app
from backend.app.models import PlatformAccount, User
from backend.app.services import proactive_interactions as service


class Platform:
    writes = []
    failure = False

    def __init__(self, cookies):
        pass

    def get_self_info(self):
        return {'user_id': 'self'}

    def get_homefeed(self):
        return {'items': [self.card('n', 'peer'), self.card('own', 'self')]}

    @staticmethod
    def card(note_id, author):
        return {'id': note_id, 'note_card': {'note_id': note_id, 'title': '山里露营', 'desc': '山里起雾，帐篷有点结露', 'user': {'user_id': author, 'nickname': '作者'}}}

    def get_note_info(self, url):
        return True, '', {'data': {'items': [self.card('n', 'peer')]}}

    def get_comment_preview(self, url):
        return ['营地晚上冷吗？', '这地方好美']

    def get_mentions(self, cursor):
        return {'message_list': [{'type': 'comment/note', 'user_info': {'user_id': 'peer'}}, {'type': 'mention/user', 'user_info': {'user_id': 'unrelated'}}]}

    def get_likes(self, cursor):
        return {'message_list': [{'type': 'liked/item', 'user_info': {'userid': 'peer'}}, {'type': 'liked/item', 'user_info': {'userid': 'self'}}]}

    def get_recent_user_notes(self, peer):
        assert peer == 'peer'
        return {'notes': [self.card('n', peer)]}

    def post_comment(self, note_id, text):
        self.writes.append((note_id, text))
        if self.failure:
            raise TimeoutError()
        return {'data': {'comment': {'id': 'receipt'}}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "app.sqlite3"}', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add_all([User(id=1, username='owner', password_hash='unused'), User(id=2, username='other', password_hash='unused')])
        db.add_all([PlatformAccount(id=1, user_id=1, platform='xhs', sub_type='pc', external_user_id='self', nickname='我', status='active'),
                    PlatformAccount(id=2, user_id=2, platform='xhs', sub_type='pc', external_user_id='other', nickname='其他', status='active')])
        db.commit()
    monkeypatch.setattr(service, 'STORE', tmp_path / 'proactive.sqlite3')
    monkeypatch.setattr(service, 'SessionLocal', sessions)
    monkeypatch.setattr(service, 'XhsPcApiAdapter', Platform)
    monkeypatch.setattr(service, '_get_owned_pc_account_cookies', lambda *args: 'never-expose-cookie')
    monkeypatch.setattr(service, 'ensure_account_config', lambda *args: SimpleNamespace())
    monkeypatch.setattr(service, 'stopped_peers', lambda account: set())
    monkeypatch.setattr(service.xhs_network, 'require_ready', lambda: None)
    monkeypatch.setattr(service, 'decide', lambda config, context: {'note_id': context['notes'][0]['note_id'], 'text': '起雾时帐篷结露明显吗？', 'reason': '围绕笔记里的结露问题交流'})
    Platform.writes = []; Platform.failure = False
    def test_db():
        with sessions() as db:
            yield db
    app = create_app(); app.dependency_overrides[get_db] = test_db
    yield TestClient(app), sessions
    engine.dispose()


def headers(owner=1):
    return {'Authorization': 'Bearer ' + create_access_token(owner)}


def configure(client, **changes):
    source = changes.get('source', 'recommend')
    body = {**service.DEFAULTS, **changes}
    response = client.put('/api/xhs/proactive/1/settings/' + source, json=body, headers=headers())
    assert response.status_code == 200, response.text
    return response.json()


def start(client, **changes):
    configure(client, **changes)
    response = client.post('/api/xhs/proactive/1/run/' + changes.get('source', 'recommend'), headers=headers())
    assert response.status_code == 202, response.text
    return response.json()['id']


def state(client):
    response = client.get('/api/xhs/proactive/1', headers=headers())
    assert response.status_code == 200
    assert 'never-expose-cookie' not in response.text
    return response.json()


def test_auth_and_ownership(setup):
    client, _ = setup
    for method, path in [('get', '/api/xhs/proactive/1'), ('post', '/api/xhs/proactive/1/run/recommend'), ('put', '/api/xhs/proactive/1/settings/recommend')]:
        kwargs = {'json': service.DEFAULTS} if method == 'put' else {}
        assert getattr(client, method)(path, **kwargs).status_code == 401
        assert getattr(client, method)(path, headers=headers(2), **kwargs).status_code == 404
    assert client.put('/api/xhs/proactive/1/settings/recommend', json={**service.DEFAULTS, 'interval_minutes': 0}, headers=headers()).status_code == 422


def test_direct_send_records_content_time_and_cannot_resend(setup):
    client, _ = setup
    start(client)
    assert client.post('/api/xhs/proactive/1/run/recommend', headers=headers()).status_code == 409
    service.tick()
    item = state(client)['items'][0]
    assert item['state'] == 'submitted' and len(item['references']) == 2
    assert item['sent_at'] and item['account_nickname'] == '我' and item['source'] == 'recommend'
    assert item['text'] == Platform.writes[0][1]
    with pytest.raises(HTTPException):
        service.send_item(1, 1, item['id'], automatic=True)
    start(client); service.tick()
    assert state(client)['runs'][0]['state'] == 'skipped'
    assert len(Platform.writes) == 1
    assert client.post('/api/xhs/proactive/1/items/' + item['id'] + '/send', headers=headers()).status_code in (404, 405)


def test_auto_send_and_uncertain_transport_never_replayed(setup):
    client, _ = setup
    Platform.failure = True
    start(client)
    service.tick(); service.tick()
    result = state(client)
    assert result['runs'][0]['state'] == 'uncertain'
    assert result['items'][0]['state'] == 'uncertain'
    path = '/api/xhs/proactive/1/items/' + result['items'][0]['id'] + '/send'
    assert client.post(path, headers=headers()).status_code in (404, 405)
    assert len(Platform.writes) == 1


def test_pause_before_model_returns_prevents_automatic_send(setup, monkeypatch):
    client, _ = setup
    original = service.decide
    def decide(config, context):
        configure(client, enabled=False)
        return original(config, context)
    monkeypatch.setattr(service, 'decide', decide)
    start(client); service.tick()
    assert state(client)['items'][0]['state'] == 'cancelled'
    assert not Platform.writes


def test_visitors_merge_sources_and_exclude_self(setup):
    client, _ = setup
    start(client, source='visitors'); service.tick()
    result = state(client)
    assert result['items'][0]['state'] == 'submitted'
    assert len(Platform.writes) == 1


def test_schedule_runs_once_and_does_not_catch_up_or_switch_identity(setup):
    client, sessions = setup
    configure(client, enabled=True)
    with service.store() as db:
        db.execute("UPDATE strategy_settings SET next_at='2000-01-01'")
    service.tick(); service.tick()
    assert len(state(client)['runs']) == 1 and len(Platform.writes) == 1
    with sessions() as db:
        db.get(PlatformAccount, 1).external_user_id = 'new-identity'; db.commit()
    with service.store() as db:
        db.execute("UPDATE strategy_settings SET next_at='2000-01-01'")
    service.tick()
    assert len(state(client)['runs']) == 1
    assert client.post('/api/xhs/proactive/1/run/recommend', headers=headers()).status_code == 409


def test_invalid_model_target_and_empty_decision_never_send(setup, monkeypatch):
    client, _ = setup
    monkeypatch.setattr(service, 'decide', lambda *args: {'note_id': 'invented', 'text': '嗨'})
    start(client); service.tick()
    assert state(client)['runs'][0]['state'] == 'failed' and not Platform.writes
    monkeypatch.setattr(service, 'decide', lambda *args: {'note_id': '', 'reason': '无适合的话题'})
    start(client); service.tick()
    assert state(client)['runs'][0]['state'] == 'skipped' and not Platform.writes


def test_restart_marks_inflight_uncertain_without_resending(setup):
    client, _ = setup
    run_id = start(client)
    service.tick()
    old = (service.shanghai_now() - timedelta(minutes=20)).isoformat()
    with service.store() as db:
        db.execute("UPDATE runs SET state='running',updated_at=? WHERE id=?", (old, run_id))
        db.execute("UPDATE items SET state='sending'")
    service.tick()
    result = state(client)
    assert result['runs'][0]['state'] == result['items'][0]['state'] == 'uncertain'
    assert len(Platform.writes) == 1  # Only the initial submit; restart cannot repeat it.


def test_unreadable_comments_use_note_text_and_show_limitation(setup, monkeypatch):
    client, _ = setup
    def unavailable(*args):
        raise ValueError('incomplete response')
    monkeypatch.setattr(Platform, 'get_comment_preview', unavailable)
    start(client); service.tick()
    item = state(client)['items'][0]
    assert item['state'] == 'submitted' and item['references'] == []
    assert '仅依据笔记正文' in item['reference_status']


def test_failed_note_reads_are_not_reported_as_no_candidates(setup, monkeypatch):
    client, _ = setup
    monkeypatch.setattr(Platform, 'get_note_info', lambda *args: (False, '', {}))
    start(client); service.tick()
    result = state(client)
    assert result['runs'][0]['state'] == 'failed' and not result['items']


def test_existing_stop_contact_blocks_send(setup, monkeypatch):
    client, _ = setup
    original = service.decide
    def decide(config, context):
        monkeypatch.setattr(service, 'stopped_peers', lambda account: {'peer'})
        return original(config, context)
    monkeypatch.setattr(service, 'decide', decide)
    start(client); service.tick()
    assert not Platform.writes and state(client)['runs'][0]['state'] == 'failed'


def test_model_uses_existing_numeric_comment_account_route(monkeypatch):
    def complete(config, prompt, **kwargs):
        assert kwargs['account_id'] == 7 and kwargs['category'] == 'lifestyle_editor'
        return {'structured': {'note_id': '', 'reason': 'skip'}}
    monkeypatch.setattr(service.channel_ai, 'complete', complete)
    assert service.decide(object(), {'account_id': 7})['note_id'] == ''


def test_independent_strategies_and_minute_intervals(setup):
    client, _ = setup
    first = configure(client, source='recommend', enabled=True, interval_minutes=2, browse_limit=15, comment_limit=3)
    second = configure(client, source='visitors', enabled=True, interval_minutes=5, comment_limit=2)
    result = state(client)['strategies']
    assert result['recommend']['revision'] == first['revision']
    assert result['visitors']['revision'] == second['revision']
    from datetime import datetime
    now = service.shanghai_now()
    assert 110 < (datetime.fromisoformat(first['next_at']) - now).total_seconds() <= 120
    assert 290 < (datetime.fromisoformat(second['next_at']) - now).total_seconds() <= 300
    assert 'auto_send' not in result['recommend']
    a = client.post('/api/xhs/proactive/1/run/recommend', headers=headers())
    b = client.post('/api/xhs/proactive/1/run/visitors', headers=headers())
    assert a.status_code == b.status_code == 202
    configure(client, source='visitors', enabled=False)
    assert state(client)['strategies']['recommend']['enabled'] is True


def test_configurable_browse_and_send_count(setup, monkeypatch):
    client, _ = setup
    monkeypatch.setattr(Platform, 'get_homefeed', lambda self: {'items': [self.card('n'+str(i),'peer'+str(i)) for i in range(5)]})
    calls = []
    def detail(self, url):
        note_id = url.split('/')[-1].split('?')[0]
        calls.append(note_id)
        return True, '', {'data': {'items': [self.card(note_id, 'peer'+note_id[1:])]}}
    monkeypatch.setattr(Platform, 'get_note_info', detail)
    start(client, browse_limit=3, comment_limit=2); service.tick()
    assert len(calls) == 3 and len(Platform.writes) == 2
    assert len(state(client)['items']) == 2
    assert all(item['state'] == 'submitted' for item in state(client)['items'])


def test_visit_uses_latest_work_only_even_when_unavailable(setup, monkeypatch):
    client, _ = setup
    old = Platform.card('old','peer'); old['note_card']['time'] = 10
    new = Platform.card('new','peer'); new['note_card']['time'] = 20
    monkeypatch.setattr(Platform, 'get_recent_user_notes', lambda self, peer: {'notes': [old,new]})
    calls = []
    def unavailable(self, url):
        calls.append(url)
        return False, '', {}
    monkeypatch.setattr(Platform, 'get_note_info', unavailable)
    start(client, source='visitors'); service.tick()
    assert len(calls) == 1 and '/new' in calls[0]
    assert state(client)['runs'][0]['state'] == 'failed' and not Platform.writes


def test_legacy_migration_preserves_hours_and_does_not_activate_draft_mode(setup):
    client, _ = setup
    for enabled_send in (False, True):
        with service.store() as db:
            db.execute('DELETE FROM strategy_settings')
            body = {'source':'visitors','enabled':True,'auto_send':enabled_send,'interval_hours':2,'identity':'self'}
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?,?,?,?)',(1,1,'legacy',json.dumps(body),'2099-01-01'))
        result = state(client)['strategies']
        assert result['visitors']['interval_minutes'] == 120
        assert result['visitors']['enabled'] is enabled_send
        assert result['recommend']['enabled'] is False
        assert 'auto_send' not in result['visitors']


def test_strategies_share_note_deduplication(setup):
    client, _ = setup
    start(client); service.tick()
    start(client, source='visitors'); service.tick()
    assert len(Platform.writes) == 1
    assert state(client)['runs'][0]['state'] == 'skipped'


def test_scheduler_fires_each_strategy_at_its_own_minute_interval(setup, monkeypatch):
    client, _ = setup
    base = service.shanghai_now()
    clock = [base]
    monkeypatch.setattr(service, 'shanghai_now', lambda: clock[0])
    configure(client, enabled=True, interval_minutes=2)
    configure(client, source='visitors', enabled=True, interval_minutes=5)
    fired = []
    def execute(run):
        fired.append(json.loads(run['config'])['source'])
        service.finish(run['id'], 'completed', 'test')
    monkeypatch.setattr(service, 'execute', execute)
    service.tick()
    clock[0] = base + timedelta(minutes=1); service.tick()
    assert fired == []
    clock[0] = base + timedelta(minutes=2); service.tick(); service.tick()
    assert fired == ['recommend']
    clock[0] = base + timedelta(minutes=5); service.tick(); service.tick(); service.tick()
    assert sorted(fired) == ['recommend', 'recommend', 'visitors']
    clock[0] = base + timedelta(minutes=6); service.tick()
    assert len(fired) == 3


def test_expired_identity_check_shows_relogin_instruction_without_sending(setup, monkeypatch):
    from backend.app.services.account_health import profile_failure
    client, _ = setup
    start(client)
    def expired(self):
        raise profile_failure({'code': -100, 'msg': 'secret-response-must-not-leak'})
    monkeypatch.setattr(Platform, 'get_self_info', expired)
    service.tick()
    result = state(client)
    assert result['runs'][0]['state'] == 'waiting_login'
    assert '登录已失效' in result['runs'][0]['detail']
    assert '重新登录' in result['runs'][0]['detail']
    assert 'secret-response-must-not-leak' not in str(result)
    assert not result['items'] and not Platform.writes


def test_pc_self_profile_failure_uses_safe_platform_diagnosis(monkeypatch):
    from backend.app.adapters.xhs.pc_api_adapter import XhsPcApiAdapter
    from backend.app.services.account_health import AccountCheckError
    closed = []
    fake = SimpleNamespace(get_user_me=lambda: (False, 'private-token-never-show', {'code': -100}),
                           http=SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr(XhsPcApiAdapter, '_api', lambda self: fake)
    with pytest.raises(AccountCheckError, match='登录已失效') as error:
        XhsPcApiAdapter('synthetic').get_self_info()
    assert error.value.business_code == -100
    assert 'private-token' not in str(error.value)
    assert closed == [True]


def test_expiry_creates_recovery_event_and_resumes_after_verified_login(setup, monkeypatch):
    from backend.app.models import AccountCookieVersion, ApiLog
    from backend.app.core.security import encrypt_text
    from backend.app.services.account_health import profile_failure
    from sqlalchemy import select
    client, sessions = setup
    with sessions() as db:
        db.add(AccountCookieVersion(platform_account_id=1, encrypted_cookies=encrypt_text('synthetic')))
        db.commit()
    start(client)
    original = Platform.get_self_info
    def expired(self):
        raise profile_failure({'code': -100})
    monkeypatch.setattr(Platform, 'get_self_info', expired)
    service.tick()
    assert state(client)['runs'][0]['state'] == 'waiting_login'
    with sessions() as db:
        assert db.get(PlatformAccount, 1).status == 'expired'
        event = db.scalars(select(ApiLog)).one()
        assert event.status == 'expired'
        assert event.meta['source'] == 'comment_identity_check'
        assert event.meta['business_code'] == -100
    service.tick()
    assert not Platform.writes
    with sessions() as db:
        db.get(PlatformAccount, 1).status = 'active'
        db.commit()
    monkeypatch.setattr(Platform, 'get_self_info', original)
    service.tick()
    assert state(client)['runs'][0]['state'] == 'completed'
    assert len(Platform.writes) == 1
    service.tick()
    assert len(Platform.writes) == 1


def test_waiting_login_run_cancels_when_configuration_changes(setup, monkeypatch):
    from backend.app.services.account_health import profile_failure
    client, _ = setup
    start(client)
    def expired(self):
        raise profile_failure({'code': -100})
    monkeypatch.setattr(Platform, 'get_self_info', expired)
    service.tick()
    configure(client, instruction='新的互动要求')
    service.tick()
    assert state(client)['runs'][0]['state'] == 'cancelled'
    assert not Platform.writes


def test_stale_identity_failure_does_not_expire_new_login(setup, monkeypatch):
    from backend.app.models import AccountCookieVersion, ApiLog
    from backend.app.core.security import encrypt_text
    from backend.app.services.account_health import profile_failure
    from sqlalchemy import select
    client, sessions = setup
    with sessions() as db:
        db.add(AccountCookieVersion(platform_account_id=1, encrypted_cookies=encrypt_text('old')))
        db.commit()
    start(client)
    def expired_after_relogin(self):
        with sessions() as db:
            db.add(AccountCookieVersion(platform_account_id=1, encrypted_cookies=encrypt_text('new')))
            db.commit()
        raise profile_failure({'code': -100})
    monkeypatch.setattr(Platform, 'get_self_info', expired_after_relogin)
    service.tick()
    with sessions() as db:
        assert db.get(PlatformAccount, 1).status == 'active'
        assert db.scalars(select(ApiLog)).first() is None
    assert not Platform.writes
