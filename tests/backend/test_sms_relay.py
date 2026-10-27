import base64
import hashlib
import hmac
import time
from types import SimpleNamespace
from urllib.parse import quote_plus

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.services import sms_relay as relay
from backend.app.api import sms_relay as api
from backend.app.core.deps import get_current_user


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, 'DIRECTORY', tmp_path)
    app = FastAPI()
    app.include_router(api.router, prefix='/api')
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=12)
    return TestClient(app)


def signed(user, content):
    secret = relay.settings(user)['secret']
    stamp = str(int(time.time()*1000))
    sign = base64.b64encode(hmac.new(secret.encode(), (stamp+'\n'+secret).encode(), hashlib.sha256).digest()).decode()
    return {'timestamp': stamp, 'sign': quote_plus(sign), 'content': content, 'from': 'synthetic'}


def test_webhook_auth_expiry_scope_and_test_receipt(client):
    data = signed(12, 'synthetic test message')
    assert client.post('/api/sms-relay/inbound/12', data=data).json()['state'] == 'received_test_or_ignored'
    assert relay.settings(12)['last_received']
    assert client.post('/api/sms-relay/inbound/13', data=data).status_code == 401
    data['timestamp'] = '1'
    assert client.post('/api/sms-relay/inbound/12', data=data).status_code == 401
    assert client.post('/api/sms-relay/inbound/12', data={'content':'x'}).status_code == 401
    assert client.post('/api/sms-relay/inbound/12', data=signed(12,'x'), headers={'Origin':'https://evil.test'}).status_code == 403


def test_sms_never_logs_in_without_owned_pending_request(client):
    assert client.post('/api/sms-relay/inbound/12', data=signed(12,'【小红书】验证码123456')).json()['state'] == 'no_pending_login'
    relay.register_pending(12, 1, '13800000000', None)
    assert relay.claim(12,'【小红书】验证码123456')[0] is None


def test_pending_one_time_claim_expiry_supersession_and_no_plaintext(client):
    relay.settings(12)
    relay.register_pending(12, 1, '13800000000', 2)
    relay.register_pending(12, 2, '13800000000', 2)
    assert relay.claim(12,'【其他平台】验证码123456')[0] is None
    assert relay.claim(12,'【小红书】验证码123456，验证码654321')[0] is None
    pending,state = relay.claim(12,'【小红书】验证码123456')
    assert state == 'processing' and pending['session_id'] == 2 and pending['phone'] == '13800000000'
    assert relay.claim(12,'【小红书】验证码123456')[0] is None
    relay.complete(pending,'confirmed')
    with relay.connect() as db:
        assert db.execute('SELECT state FROM pending WHERE session_id=1').fetchone()[0] == 'superseded'
    contents = (relay.DIRECTORY/'sms-relay.sqlite3').read_bytes()
    assert b'123456' not in contents and b'13800000000' not in contents
    relay.register_pending(12,3,'13800000000',2)
    with relay.connect() as db:
        db.execute('UPDATE pending SET expires=0 WHERE session_id=3')
    assert relay.claim(12,'【小红书】验证码222222')[0] is None


def test_limits_and_malformed_input(client):
    assert client.post('/api/sms-relay/inbound/12',content=b'x'*8193).status_code == 413
    assert client.post('/api/sms-relay/inbound/12',json=[]).status_code == 400
    response = client.get('/api/sms-relay/settings')
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['url'].endswith('/12')


def test_owner_sender_mapping_accepts_brandless_sms_only_from_exact_sender(client):
    relay.settings(12)
    response=client.put('/api/sms-relay/settings',json={'xhs_senders':['10690001']})
    assert response.status_code==200
    relay.register_pending(12,1,'13800000000',2)
    assert relay.claim(12,'验证码123456，请勿泄露','10690002')[0] is None
    assert relay.claim(13,'验证码123456，请勿泄露','10690001')[0] is None
    assert relay.claim(12,'普通短信123456','10690001')[0] is None
    pending,state=relay.claim(12,'验证码123456，请勿泄露','10690001')
    assert state=='processing' and pending['session_id']==1
    assert client.put('/api/sms-relay/settings',json={'xhs_senders':[' ']}).status_code==400


def test_webhook_passes_authenticated_sender_and_code(client,monkeypatch):
    calls=[]
    relay.settings(12)
    monkeypatch.setattr(api,'deliver',lambda *args: calls.append(args) or {'success':True})
    data=signed(12,'验证码123456');data['from']='10690001'
    assert client.post('/api/sms-relay/inbound/12',data=data).status_code==200
    assert calls==[(12,'验证码123456','10690001')]


def test_default_template_dates_sender_and_sim_metadata_do_not_compete_with_code(client):
    relay.settings(12);relay.register_pending(12,1,'13800000000',2)
    text='10692805402988200\n【小红书】您的验证码是: 123456，3分钟内有效。\nSIM1_\nSubId: 2\n2026-10-13 13:22:43'
    pending,state=relay.claim(12,text,'10692805402988200')
    assert state=='processing' and pending['code']=='123456'


def test_forward_to_original_session_then_never_retry_uncertain(client,monkeypatch):
    from backend.app.api import login_sessions
    relay.settings(12);relay.register_pending(12,9,'13800000000',2)
    session=SimpleNamespace(id=9,user_id=12,platform='xhs',status='pending',sub_type='pc')
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,cls,id):return session if cls is api.LoginSession else SimpleNamespace(id=12)
    monkeypatch.setattr(api,'SessionLocal',DB)
    calls=[]
    def confirm(payload,*args):
        calls.append(payload)
        raise RuntimeError('synthetic lost response')
    monkeypatch.setattr(login_sessions,'_confirm_phone_login',confirm)
    data=signed(12,'【小红书】验证码123456')
    assert client.post('/api/sms-relay/inbound/12',data=data).json()['state']=='needs_attention'
    client.post('/api/sms-relay/inbound/12',data=data)
    assert len(calls)==1 and calls[0].session_id==9 and calls[0].code=='123456'
