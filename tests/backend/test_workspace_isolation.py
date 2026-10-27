"""Independent workspace logins own independent XHS matrices; no platform calls."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.main import create_app
from backend.app.core.database import Base, get_db
from backend.app.core.security import decrypt_text, decode_token
from backend.app.core.time import shanghai_now
from backend.app.models import User, PlatformAccount, AccountCookieVersion, Task
from backend.app.models.direct_message import DmListener, DmHistory, DmConversation, DmManualMessage
from backend.app.api import accounts, model_routing, comment_monitoring
from backend.app.services import account_service, proactive_interactions, channel_ai


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path/'workspace.sqlite3'), connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    app = create_app()
    def database():
        with sessions() as db: yield db
    app.dependency_overrides[get_db] = database
    class Login:
        def get_user_info(self, cookies):
            return {'external_user_id': cookies['social_id'], 'nickname': cookies['nickname']}
    app.dependency_overrides[accounts.get_pc_account_adapter] = lambda: Login()
    app.dependency_overrides[accounts.get_xhs_self_profile_adapter] = lambda: SimpleNamespace(get_self_profile=lambda _: {})
    monkeypatch.setattr(account_service, 'record_session_event', lambda *a, **kw: None)
    monkeypatch.setattr(proactive_interactions, 'STORE', tmp_path/'proactive.sqlite3')
    monkeypatch.setattr(proactive_interactions, 'SessionLocal', sessions)
    monkeypatch.setattr(comment_monitoring, 'CONFIG', tmp_path/'missing-comment-config.json')
    media = tmp_path/'media'; media.mkdir()
    from backend.app.api import files
    monkeypatch.setattr(files, '_media_dir', lambda: media)
    a, b, anonymous = TestClient(app), TestClient(app), TestClient(app)
    def register(client, username):
        response = client.post('/api/auth/register', json={'username': username, 'password': 'synthetic-password'})
        assert response.status_code == 200
        payload = response.json()
        client.headers['Authorization'] = 'Bearer ' + payload['access_token']
        return payload
    aa, bb = register(a, 'matrix-owner-a'), register(b, 'matrix-owner-b')
    def bind(client, social_id, nickname):
        response = client.post('/api/accounts/import-cookie', json={'platform': 'xhs', 'sub_type': 'pc',
            'cookie_string': f'social_id={social_id}; nickname={nickname}; web_session=synthetic-private'})
        assert response.status_code == 200, response.text
        return response.json()['id']
    a1, a2, b1 = bind(a, 'same-social-id', 'A-primary'), bind(a, 'a-secondary', 'A-secondary'), bind(b, 'same-social-id', 'B-primary')
    with sessions() as db:
        db.add_all([DmListener(account_id=a1), DmListener(account_id=b1),
            DmHistory(account_id=a1, peer_id='peer', message_id='private-a', direction='inbound', text='A-only private conversation',sent_at=shanghai_now()),
            DmHistory(account_id=b1, peer_id='peer', message_id='private-b', direction='inbound', text='B-only private conversation',sent_at=shanghai_now()),
            DmConversation(account_id=a1,peer_id='peer'), DmConversation(account_id=b1,peer_id='peer')])
        task=Task(user_id=aa['user']['id'],platform='xhs',task_type='synthetic-private',status='completed',payload={'secret':'A-only task'})
        db.add(task); db.commit(); task_id=task.id
    yield SimpleNamespace(a=a,b=b,anonymous=anonymous,a1=a1,a2=a2,b1=b1,aa=aa,bb=bb,
                          sessions=sessions,media=media,task_id=task_id,app=app)
    a.close();b.close();anonymous.close();engine.dispose()


def test_registered_users_have_independent_matrices_even_for_same_social_identity(workspace):
    w=workspace
    assert w.a1 != w.b1
    assert {x['id'] for x in w.a.get('/api/accounts').json()['items']} == {w.a1,w.a2}
    assert {x['id'] for x in w.b.get('/api/accounts').json()['items']} == {w.b1}
    with w.sessions() as db:
        assert db.get(PlatformAccount,w.a1).user_id == w.aa['user']['id']
        assert db.get(PlatformAccount,w.b1).user_id == w.bb['user']['id']
    assert {x['account_id'] for x in w.b.get('/api/xhs/direct-messages/listeners').json()['items']} == {w.b1}
    assert 'A-only' not in w.b.get('/api/xhs/chat/messages',params={'account_id':w.b1,'peer_id':'peer'}).text
    assert 'B-only' not in w.a.get('/api/xhs/chat/messages',params={'account_id':w.a1,'peer_id':'peer'}).text


@pytest.mark.parametrize('path', [
    '/api/xhs/direct-messages?account_id={id}', '/api/xhs/direct-messages/history?account_id={id}&peer_id=peer',
    '/api/xhs/chat/conversations?account_id={id}', '/api/xhs/chat/messages?account_id={id}&peer_id=peer',
    '/api/xhs/comments/monitoring?account_id={id}', '/api/xhs/proactive/{id}',
    '/api/xhs/proactive/{id}/agents', '/api/model-routing?account_id={id}',
])
def test_guessing_another_matrix_id_cannot_read_data(workspace,path):
    w=workspace
    assert w.b.get(path.format(id=w.a1)).status_code == 404


@pytest.mark.parametrize('method,path,body', [
    ('delete','/api/accounts/{id}',None), ('patch','/api/accounts/{id}',None),
    ('post','/api/accounts/{id}/check',None),
    ('put','/api/xhs/direct-messages/listeners/{id}',{'enabled':False}),
    ('post','/api/xhs/chat/accounts/{id}/connect',{'enabled':True}),
    ('post','/api/xhs/chat/accounts/{id}/refresh',{}),
    ('post','/api/xhs/chat/accounts/{id}/control',{'peer_id':'peer','enabled':True}),
    ('put','/api/xhs/proactive/{id}/settings/recommend',{'enabled':True,'instruction':'intruder'}),
    ('post','/api/xhs/proactive/{id}/run/recommend',None),
    ('put','/api/xhs/comments/listeners/{id}',{'enabled':False,'auto_reply':False,'control_mode':'full_access'}),
])
def test_guessing_another_matrix_id_cannot_modify_or_send(workspace,method,path,body):
    w=workspace
    response=getattr(w.b,method)(path.format(id=w.a1),**({'json':body} if body is not None else {}))
    assert response.status_code == 404,response.text
    with w.sessions() as db:
        assert db.get(PlatformAccount,w.a1) and not db.get(DmListener,w.a1).enabled
        assert not list(db.scalars(select(DmManualMessage)))


def test_manual_message_target_is_owned_before_any_queue_write(workspace):
    w=workspace
    response=w.b.post(f'/api/xhs/chat/accounts/{w.a1}/send',json={
        'client_id':'intruder-send','expected_external_user_id':'same-social-id','peer_id':'peer','text':'must never queue'})
    assert response.status_code==404
    assert w.b.get('/api/tasks/'+str(w.task_id)).status_code==404
    assert w.b.get('/api/tasks').json()['items']==[]


def test_strategy_preferences_and_runs_are_owned_per_matrix(workspace):
    w=workspace
    for client,account,instruction in ((w.a,w.a1,'A-only preference'),(w.b,w.b1,'B-only preference')):
        assert client.put(f'/api/xhs/proactive/{account}/settings/recommend',json={'instruction':instruction}).status_code==200
    assert w.a.get(f'/api/xhs/proactive/{w.a1}').json()['strategies']['recommend']['instruction']=='A-only preference'
    assert w.b.get(f'/api/xhs/proactive/{w.b1}').json()['strategies']['recommend']['instruction']=='B-only preference'


def test_stale_strategy_owner_is_not_exposed_by_a_reused_account_reference(workspace):
    w=workspace
    assert w.a.put(f'/api/xhs/proactive/{w.a1}/settings/recommend',json={'instruction':'stale private preference'}).status_code==200
    with proactive_interactions.store() as db:
        db.execute('UPDATE strategy_settings SET owner_id=? WHERE account_id=?', (w.bb['user']['id'],w.a1))
    own=w.a.get(f'/api/xhs/proactive/{w.a1}').json()['strategies']['recommend']
    assert own['instruction']=='' and own['revision']==''


def test_deleting_matrix_removes_its_manual_messages_and_never_reuses_its_id(workspace):
    w=workspace
    with w.sessions() as db:
        db.add(DmManualMessage(account_id=w.b1,client_id='private-old',external_user_id='same-social-id',
            peer_id='peer',text='old owner private draft',outbound_mid='private-mid',state='queued'))
        db.commit()
    assert w.b.delete(f'/api/accounts/{w.b1}').status_code==200
    with w.sessions() as db:
        assert not list(db.scalars(select(DmManualMessage).where(DmManualMessage.account_id==w.b1)))
    response=w.a.post('/api/accounts/import-cookie',json={'platform':'xhs','sub_type':'pc',
        'cookie_string':'social_id=new-social; nickname=A-new; web_session=synthetic'})
    assert response.status_code==200 and response.json()['id']>w.b1


def test_media_requires_login_and_ownership_and_browser_cookie_is_read_only(workspace):
    w=workspace
    filename=f"xhs-upload-u{w.aa['user']['id']}-private.png"
    (w.media/filename).write_bytes(b'synthetic-private-image')
    url='/api/files/media/'+filename
    assert w.anonymous.get(url).status_code==401
    assert w.b.get(url).status_code==404
    owner=w.a.get(url);assert owner.status_code==200 and owner.headers['cache-control']=='private, no-store'
    # A normal image tag sends only cookies; other APIs still require a bearer token.
    del w.a.headers['Authorization']
    assert w.a.get(url).status_code==200
    assert w.a.get('/api/accounts').status_code==401


def test_logout_revokes_access_refresh_and_media_cookie_without_affecting_other_user(workspace):
    w=workspace
    old=w.aa
    assert w.a.post('/api/auth/logout').status_code==200
    assert w.a.get('/api/accounts').status_code==401
    assert w.anonymous.post('/api/auth/refresh',json={'refresh_token':old['refresh_token']}).status_code==401
    assert w.b.get('/api/accounts').status_code==200
    login=w.a.post('/api/auth/login',json={'username':'matrix-owner-a','password':'synthetic-password'})
    assert login.status_code==200
    assert decode_token(login.json()['access_token'])['auth_version']==1
    w.a.headers['Authorization']='Bearer '+login.json()['access_token']
    assert {x['id'] for x in w.a.get('/api/accounts').json()['items']}=={w.a1,w.a2}


def test_agent_routes_do_not_expose_other_matrix_account_selectors(workspace,monkeypatch):
    w=workspace
    def route(name,selectors):
        return {'id':name,'name':name,'account_ids':selectors,'agent':{'id':name,'name':name},
            'categories':['content_analysis'],'surfaces':['manual'],'event_types':['manual.request'],'kinds':['text']}
    routes=[route('A-only-route',[str(w.a1)]),route('B-only-route',[str(w.b1)]),route('shared',['*',str(w.a1)])]
    monkeypatch.setattr(model_routing,'channel_client',lambda:SimpleNamespace(catalog=lambda:{'routes':routes,'connected':True}))
    result=w.b.get('/api/model-routing')
    assert result.status_code==200 and 'A-only-route' not in result.text
    assert {r['id'] for r in result.json()['routes']}=={'B-only-route','shared'}
    assert next(r for r in result.json()['routes'] if r['id']=='shared')['account_ids']==['*']
    monkeypatch.setattr(model_routing,'validate_route',lambda *args:routes[0])
    assert w.b.put('/api/model-routing/text',json={'route_id':'A-only-route'}).status_code==404


@pytest.mark.parametrize('path', ['/api/accounts','/api/xhs/direct-messages/listeners',
    '/api/xhs/pc/search/users','/api/xhs/pc/homefeed/channels','/api/xhs/pc/homefeed/recommend'])
def test_anonymous_workspace_data_access_is_rejected(workspace,path):
    # Existing routes can return 404 when unsupported; they must never expose data.
    assert workspace.anonymous.get(path).status_code in (401,404,405)


def test_ai_cannot_read_another_workspace_users_local_image(tmp_path,monkeypatch):
    class Client:
        def upload(self,*args):raise AssertionError('foreign bytes must never leave the server')
    monkeypatch.setattr(channel_ai,'channel_client',lambda:Client())
    config=SimpleNamespace(user_id=2,base_url='',model_name='synthetic')
    with pytest.raises(ValueError,match='不属于当前用户'):
        channel_ai.complete(config,'inspect',images=['/api/files/media/xhs-upload-u1-private.png'])


def test_production_media_cookie_is_secure_http_only_and_scoped(workspace):
    client=TestClient(workspace.app,base_url='https://testserver')
    try:
        response=client.post('/api/auth/login',json={'username':'matrix-owner-a','password':'synthetic-password'})
        assert response.status_code==200
        cookie=response.headers['set-cookie']
        assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie
        assert 'Path=/api/files/media' in cookie
        assert response.headers['cache-control']=='private, no-store'
    finally:client.close()
