"""Read-only monitoring must enforce account ownership and omit private input."""
from datetime import timedelta
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.api import comment_monitoring as api
from backend.app.core.database import Base, get_db
from backend.app.core.security import create_access_token
from backend.app.main import create_app
from backend.app.models import PlatformAccount, User
from backend.app.services.content_automation import Ledger, now
from backend.app.services.content_automation import comment_settings_key, comment_reply_version
from backend.app.services.channel_client import ChannelError


@pytest.fixture
def monitoring(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "test.sqlite3"}', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine); sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add_all([User(id=1, username='owner', password_hash='unused'), User(id=2, username='other', password_hash='unused')])
        db.add_all([PlatformAccount(id=2, user_id=1, platform='xhs', sub_type='pc', external_user_id='peach', nickname='小桃', status='active'),
                    PlatformAccount(id=3, user_id=2, platform='xhs', sub_type='pc', external_user_id='other', nickname='其他'),
                    PlatformAccount(id=4, user_id=1, platform='xhs', sub_type='pc', external_user_id='leo', nickname='Leo', status='active')]); db.commit()
    def get_test_db():
        with sessions() as db: yield db
    config = tmp_path / 'config.json'; config.write_text(json.dumps({'user_id': 1, 'pc_account_id': 2, 'expected_external_user_id': 'peach', 'enabled': True, 'route_id': 'comments'}))
    monkeypatch.setattr(api, 'CONFIG', config); monkeypatch.setattr(api, 'PRIVATE', tmp_path)
    ledger = Ledger(tmp_path / 'content-automation.sqlite3')
    ledger.set_state('heartbeat', now().isoformat()); ledger.set_state('execution', 'active')
    ledger.set_state('comment_health', {'state': 'listening', 'checked_at': now().isoformat()})
    ledger.add('comment:2:c', 'comment', {'text': '优秀👍', 'note_id': 'n', 'comment_id': 'c', 'peer_id': 'reader',
        'raw': {'cookie': 'private-cookie', 'xsec_token': 'private-token'}, 'request': {'secret': 'private-request'},
        'decision': {'decision': 'no_reply', 'reason': '重复刷屏'}})
    ledger.update('comment:2:c', stage='no_reply')
    app = create_app(); app.dependency_overrides[get_db] = get_test_db
    yield TestClient(app), ledger
    ledger.db.close(); engine.dispose()


def test_expired_account_is_reported_even_with_cached_listening_health_and_recovers(monitoring):
    client, ledger = monitoring
    headers = {'Authorization': 'Bearer ' + create_access_token(1)}
    endpoint = '/api/xhs/comments/monitoring?account_id=2'
    def change_status(value):
        generator = client.app.dependency_overrides[get_db]()
        try:
            db = next(generator)
            db.get(PlatformAccount, 2).status = value
            db.commit()
        finally:
            generator.close()
    change_status('expired')
    result = client.get(endpoint, headers=headers).json()
    assert result['state'] == 'login_expired' and result['enabled'] is True
    assert result['account_status'] == 'expired' and '重新登录' in result['account_problem']
    assert not result['error']
    ledger.set_state('comment_health', {'state': 'login_expired', 'error': 'AccountUnavailable', 'checked_at': now().isoformat()})
    change_status('active')
    result = client.get(endpoint, headers=headers).json()
    assert result['state'] == 'starting' and not result['account_problem'] and not result['error']
    ledger.set_state('comment_health', {'state': 'listening', 'checked_at': now().isoformat()})
    assert client.get(endpoint, headers=headers).json()['state'] == 'listening'
    ledger.set_state(comment_settings_key({'user_id': 1, 'pc_account_id': 2, 'expected_external_user_id': 'peach'}), {'enabled': False})
    change_status('expired')
    assert client.get(endpoint, headers=headers).json()['state'] == 'stopped'


def test_monitor_requires_auth_and_owned_pc_account(monitoring):
    client, ledger = monitoring
    assert client.get('/api/xhs/comments/monitoring?account_id=2').status_code == 401
    headers = {'Authorization': 'Bearer ' + create_access_token(2)}
    assert client.get('/api/xhs/comments/monitoring?account_id=2', headers=headers).status_code == 404
    result = client.get('/api/xhs/comments/monitoring?account_id=3', headers=headers)
    assert result.status_code == 200 and result.json()['configured'] is False and not result.json()['items']


def test_monitor_shows_reason_and_stale_health_without_private_payload(monitoring):
    client, ledger = monitoring; headers = {'Authorization': 'Bearer ' + create_access_token(1)}
    response = client.get('/api/xhs/comments/monitoring?account_id=2', headers=headers)
    assert response.status_code == 200
    result = response.json()
    assert result['state'] == 'listening' and result['pending'] == 0
    assert result['items'][0]['text'] == '优秀👍' and result['items'][0]['reason'] == '重复刷屏'
    assert all(secret not in response.text for secret in ('private-cookie', 'private-token', 'private-request', 'raw', 'request'))
    ledger.set_state('heartbeat', (now() - timedelta(minutes=5)).isoformat())
    assert client.get('/api/xhs/comments/monitoring?account_id=2', headers=headers).json()['state'] == 'stale'
    ledger.set_state('execution', 'paused')
    assert client.get('/api/xhs/comments/monitoring?account_id=2', headers=headers).json()['state'] == 'paused'


def test_monitor_never_counts_receipt_or_removed_test_as_visible(monitoring):
    client, ledger = monitoring; headers = {'Authorization': 'Bearer ' + create_access_token(1)}
    row = ledger.entry('comment:2:c'); payload = json.loads(row['payload'])
    ledger.update(row['id'], stage='replied')
    result = client.get('/api/xhs/comments/monitoring?account_id=2', headers=headers).json()
    assert result['verified_visible'] == 0 and result['items'][0]['stage'] == 'submitted'
    payload['verification'] = {'state': 'not_visible', 'observer_role': 'recipient', 'sender_visible': True, 'observer_visible': False}
    ledger.update(row['id'], stage='not_visible', payload=payload)
    ledger.set_state('comment_delivery', {'blocked': False, 'has_visibility_issue': True, 'reason': '接收方不可见，自动回复继续'})
    ledger.add('test', 'comment', {}); ledger.update('test', stage='replied', detail='历史验收记录：已清理')
    result = client.get('/api/xhs/comments/monitoring?account_id=2', headers=headers).json()
    assert len(result['items']) == 1 and not result['sending_paused'] and result['verified_visible'] == 0
    assert result['visibility_warning'] == '接收方不可见，自动回复继续'
    assert result['items'][0]['verification']['observer_role'] == 'recipient'
    assert result['items'][0]['stage'] == 'not_visible'


def listener_request(**changes):
    return {'enabled': True, 'auto_reply': True, 'control_mode': 'request_approval', 'route_id': 'comments', **changes}


def fake_catalog(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(api, 'channel_client', lambda: SimpleNamespace(catalog=lambda: {'connected': True, 'routes': [
        {'id': 'comments', 'name': '评论编辑', 'agent': {'id': 'comment-agent', 'name': '编辑'}, 'categories': ['lifestyle_editor'],
         'surfaces': ['manual'], 'event_types': ['manual.request'], 'kinds': ['text']},
        {'id': 'private-dm', 'name': '私信', 'agent': {'id': 'dm-agent', 'name': '私信编辑'}, 'categories': ['reply_draft'],
         'surfaces': ['dm'], 'event_types': ['dm.received'], 'kinds': ['text']},
    ]}))


def test_listener_settings_are_owned_persistent_and_independent(monitoring, monkeypatch):
    client, ledger = monitoring; fake_catalog(monkeypatch)
    headers = {'Authorization': 'Bearer ' + create_access_token(1)}
    endpoint = '/api/xhs/comments/listeners/2'
    assert client.put(endpoint, json=listener_request()).status_code == 401
    other = {'Authorization': 'Bearer ' + create_access_token(2)}
    assert client.put(endpoint, json=listener_request(), headers=other).status_code == 404
    assert client.put('/api/xhs/comments/listeners/3', json=listener_request(), headers=other).status_code == 409
    assert client.put(endpoint, json=listener_request(route_id='private-dm'), headers=headers).status_code == 400
    assert client.put(endpoint, json=listener_request(control_mode='bogus'), headers=headers).status_code == 422
    result = client.put(endpoint, json=listener_request(auto_reply=False), headers=headers)
    assert result.status_code == 200
    assert result.json()['control_mode'] == 'request_approval' and not result.json()['auto_reply']
    config_before = api.CONFIG.read_text(); ledger.set_state('schedule', {'day': 'unchanged'})
    def unavailable(): raise ChannelError('synthetic unavailable')
    monkeypatch.setattr(api, 'channel_client', unavailable)
    assert client.put(endpoint, json=listener_request(enabled=False), headers=headers).status_code == 200
    result = client.get('/api/xhs/comments/monitoring?account_id=2', headers=headers).json()
    assert result['enabled'] is False and result['state'] == 'stopped'
    assert api.CONFIG.read_text() == config_before and ledger.state('schedule') == {'day': 'unchanged'}
    fake_catalog(monkeypatch)
    assert client.put(endpoint, json=listener_request(), headers=headers).json()['enabled'] is True
    assert ledger.state('comment_poll_at') == 0
    routes = client.get('/api/xhs/comments/routes?account_id=2', headers=headers).json()
    assert [r['id'] for r in routes['routes']] == ['comments']


def test_approval_binds_exact_reply_policy_and_cannot_resend(monitoring, monkeypatch):
    client, ledger = monitoring; fake_catalog(monkeypatch)
    headers = {'Authorization': 'Bearer ' + create_access_token(1)}
    client.put('/api/xhs/comments/listeners/2', json=listener_request(), headers=headers)
    payload = {'note_id': 'note', 'comment_id': 'c', 'peer_id': 'reader', 'reply': {'text': '谢谢呀'},
               'comment_policy': {'route_id': 'comments', 'control_mode': 'request_approval', 'agent_id': 'comment-agent',
                               'agent_snapshot': {'id': 'comment-agent', 'name': '编辑', 'route_id': 'comments', 'route_version': None}}}
    ledger.update('comment:2:c', stage='approval_pending', payload=payload)
    endpoint = '/api/xhs/comments/listeners/2/entries/comment:2:c/approve'
    old_version = comment_reply_version(payload)
    payload['reply']['text'] = '新的正文'
    ledger.update('comment:2:c', payload=payload)
    assert client.post(endpoint, json={'reply_version': old_version}, headers=headers).status_code == 409
    version = comment_reply_version(payload)
    assert client.post(endpoint, json={'reply_version': version}).status_code == 401
    other = {'Authorization': 'Bearer ' + create_access_token(2)}
    assert client.post(endpoint, json={'reply_version': version}, headers=other).status_code == 404
    assert client.post(endpoint, json={'reply_version': version}, headers=headers).status_code == 200
    assert json.loads(ledger.entry('comment:2:c')['payload'])['approval']['reply_version'] == version
    ledger.update('comment:2:c', stage='submitted')
    assert client.post(endpoint, json={'reply_version': version}, headers=headers).status_code == 409
    assert client.post(endpoint.replace('/approve', '/skip'), headers=headers).status_code == 409
    ledger.update('comment:2:c', stage='approval_pending')
    client.put('/api/xhs/comments/listeners/2', json=listener_request(control_mode='full_access'), headers=headers)
    assert client.post(endpoint, json={'reply_version': version}, headers=headers).status_code == 409
    assert client.post(endpoint.replace('/approve', '/skip'), headers=headers).status_code == 200
    assert ledger.entry('comment:2:c')['stage'] == 'no_reply'


def test_another_owned_pc_account_can_configure_without_sharing_queue(monitoring, monkeypatch):
    client, ledger = monitoring; fake_catalog(monkeypatch)
    headers = {'Authorization': 'Bearer ' + create_access_token(1)}
    view = '/api/xhs/comments/monitoring?account_id=4'
    initial = client.get(view, headers=headers).json()
    assert initial['configured'] and not initial['enabled'] and initial['state'] == 'stopped'
    assert initial['items'] == [] and initial['auto_reply'] is False
    assert client.get('/api/xhs/comments/routes?account_id=4', headers=headers).status_code == 200
    configured = client.put('/api/xhs/comments/listeners/4', json=listener_request(auto_reply=False), headers=headers)
    assert configured.status_code == 200 and configured.json()['enabled']
    assert configured.json()['items'] == [] and configured.json()['pending'] == 0
    started = configured.json()['started_at']
    assert client.post('/api/xhs/comments/listeners/4/entries/comment:2:c/skip', headers=headers).status_code == 404
    client.put('/api/xhs/comments/listeners/4', json=listener_request(enabled=False), headers=headers)
    client.put('/api/xhs/comments/listeners/4', json=listener_request(auto_reply=False), headers=headers)
    assert client.get(view, headers=headers).json()['started_at'] == started
    peach = client.get('/api/xhs/comments/monitoring?account_id=2', headers=headers).json()
    assert peach['enabled'] and peach['control_mode'] == 'full_access' and len(peach['items']) == 1
