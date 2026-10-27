import json
from unittest.mock import Mock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.app.api import account_maintenance as api
from backend.app.core.database import Base,get_db
from backend.app.models import User,PlatformAccount,ApiLog

@pytest.fixture
def control(tmp_path,monkeypatch):
    (tmp_path/'account-maintenance.json').write_text(json.dumps({'key':'test-key','user_id':1,'event_after':0}))
    monkeypatch.setattr(api,'DIRECTORY',tmp_path)
    engine=create_engine('sqlite:///'+str(tmp_path/'source.sqlite3'),connect_args={'check_same_thread':False});Base.metadata.create_all(engine);factory=sessionmaker(bind=engine)
    with factory() as db:
        db.add(User(id=1,username='owner',password_hash='fixture'))
        db.add(PlatformAccount(id=1,user_id=1,platform='xhs',sub_type='pc',external_user_id='owned',nickname='owner',status='active'));db.commit()
    def database():
        with factory() as db:yield db
    app=FastAPI();app.include_router(api.router,prefix='/api');app.dependency_overrides[get_db]=database
    return TestClient(app),factory

H={'Authorization':'Bearer test-key'}
BODY={'request_id':'request','account_id':'1','external_id':'owned','phone':'13800000000'}

def test_machine_scope_and_existing_valid_cookie_does_not_send(control,monkeypatch):
    client,_=control;check=Mock(return_value={'status':'active'});send=Mock();monkeypatch.setattr(api.accounts,'check_account',check);monkeypatch.setattr(api.login_sessions,'pc_phone_send_code',send)
    assert client.get('/api/maintenance/accounts').status_code==401
    assert client.get('/api/maintenance/accounts',headers={**H,'Origin':'https://evil.example'}).status_code==401
    assert client.get('/api/maintenance/accounts',headers=H).json()['items'][0]['external_id']=='owned'
    assert client.post('/api/maintenance/start',headers=H,json=BODY).json()['state']=='confirmed'
    assert client.post('/api/maintenance/start',headers=H,json=BODY).json()['state']=='confirmed'
    assert check.call_count==1;send.assert_not_called()
    assert client.post('/api/maintenance/start',headers=H,json={**BODY,'phone':'13900000000'}).status_code==409
    assert client.post('/api/maintenance/start',headers=H,json={**BODY,'external_id':'other'}).status_code==409

def test_events_are_committed_owned_and_skip_periodic_checks(control):
    client,factory=control
    with factory() as db:
        db.add(ApiLog(user_id=1,platform='xhs',endpoint='internal/xhs/account-session',status='expired',meta={'account_id':1,'external_id':'owned','source':'dm_websocket'}));db.commit()
    with factory() as db:
        db.add(ApiLog(user_id=1,platform='xhs',endpoint='internal/xhs/account-session',status='risk',meta={'account_id':1,'external_id':'owned','source':'creator_upload'}));db.flush();db.rollback()
    with factory() as db:
        db.add(ApiLog(user_id=1,platform='xhs',endpoint='internal/xhs/account-session',status='expired',meta={'account_id':1,'external_id':'owned','source':'scheduled_check'}));db.commit()
    rows=client.get('/api/maintenance/events',headers=H).json()['items']
    assert len(rows)==2 and rows[0]['state']=='expired' and rows[0]['ignored'] is False
    assert rows[1]['ignored'] is True
    assert client.get('/api/maintenance/events?after='+str(rows[-1]['cursor']),headers=H).json()['items']==[]

def test_forced_login_reissues_sms_and_is_idempotent(control,monkeypatch):
    client,_=control;check=Mock(return_value={'status':'active'});send=Mock(return_value={'session_id':22})
    monkeypatch.setattr(api.accounts,'check_account',check);monkeypatch.setattr(api.login_sessions,'pc_phone_send_code',send)
    payload={**BODY,'force_login':True}
    assert client.post('/api/maintenance/start',headers=H,json=payload).json()['state']=='waiting'
    assert client.post('/api/maintenance/start',headers=H,json=payload).json()['state']=='waiting'
    check.assert_not_called();assert send.call_count==1
    assert client.post('/api/maintenance/start',headers=H,json=BODY).status_code==409


def test_listener_repair_checks_identity_without_sending_sms(control,monkeypatch):
    client,_=control;check=Mock(return_value={'status':'active'});send=Mock()
    monkeypatch.setattr(api.accounts,'check_account',check);monkeypatch.setattr(api.login_sessions,'pc_phone_send_code',send)
    result=client.post('/api/maintenance/repair-listener',headers=H,json={'account_id':'1','external_id':'owned','component':'account'})
    assert result.json()['state']=='connected';send.assert_not_called()
    assert client.post('/api/maintenance/repair-listener',headers=H,json={'account_id':'1','external_id':'wrong'}).status_code==409


def test_listener_disconnect_outbox_is_durable_and_transition_deduplicated(control,monkeypatch):
    client,factory=control
    from backend.app.services import dm_worker
    from backend.app.models.direct_message import DmListener
    monkeypatch.setattr(dm_worker,'SessionLocal',factory)
    with factory() as db:
        db.add(DmListener(account_id=1,enabled=True,state='listening'));db.commit()
    dm_worker.state(1,'reconnecting','synthetic transport timeout')
    dm_worker.state(1,'reconnecting','another retry')
    rows=client.get('/api/maintenance/events',headers=H).json()['items']
    assert rows==[]
    dm_worker.state(1,'listening')
    rows=client.get('/api/maintenance/events',headers=H).json()['items']
    assert len(rows)==1 and rows[-1]['state']=='connected'


def test_manual_account_checks_and_network_logs_do_not_submit_faults(control):
 client,factory=control
 with factory() as db:
  for source,status in [('manual_check','expired'),('dm_listener','disconnected'),('scheduled_check','risk')]:
   db.add(ApiLog(user_id=1,platform='xhs',endpoint='internal/xhs/account-session',status=status,meta={'account_id':1,'external_id':'owned','source':source}));
  db.commit()
 assert all(row['ignored'] for row in client.get('/api/maintenance/events',headers=H).json()['items'])


def test_real_publish_preflight_reports_structured_account_fault(control):
 client,factory=control
 with factory() as db:
  db.add(ApiLog(user_id=1,platform='xhs',endpoint='internal/xhs/account-session',status='risk',
    meta={'account_id':1,'external_id':'owned','source':'publisher_preflight','component':'publisher','business_code':906}));db.commit()
 rows=client.get('/api/maintenance/events',headers=H).json()['items']
 assert len(rows)==1 and rows[0]['ignored'] is False
 assert rows[0]['component']=='publisher' and rows[0]['business_code']==906


def test_security_verification_pauses_automatic_sms_confirmation(control, monkeypatch):
    from fastapi import HTTPException
    from backend.app.models import LoginSession
    from backend.app.services import sms_relay
    import sqlite3
    from contextlib import contextmanager
    client, factory = control
    with factory() as db:
        db.add(LoginSession(id=22, user_id=1, platform='xhs', sub_type='pc',
                            login_method='phone', status='pending'))
        db.commit()
    monkeypatch.setattr(api.login_sessions, 'pc_phone_send_code', Mock(return_value={'session_id': 22}))
    payload = {**BODY, 'force_login': True}
    assert client.post('/api/maintenance/start', headers=H, json=payload).json()['state'] == 'waiting'
    confirm = Mock(side_effect=HTTPException(400, '小红书要求额外安全验证（HTTP 471）'))
    monkeypatch.setattr(api.login_sessions, '_confirm_phone_login', confirm)
    @contextmanager
    def relay():
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE pending(session_id INTEGER, state TEXT)')
        yield db
        db.close()
    monkeypatch.setattr(sms_relay, 'connect', relay)
    response = client.post('/api/maintenance/confirm', headers=H, json={**payload, 'code': '123456'})
    assert response.json()['state'] == 'needs_user'
    assert 'HTTP 471' in response.json()['reason']
    retry = client.post('/api/maintenance/confirm', headers=H, json={**payload, 'code': '123456'})
    assert retry.json()['state'] == 'needs_user'
    assert confirm.call_count == 1
