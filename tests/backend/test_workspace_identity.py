from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.app.core.database import Base,get_db
from backend.app.core.security import decode_token
from backend.app.api.auth import router

def make_client(tmp_path):
 engine=create_engine('sqlite:///'+str(tmp_path/'identity.db'),connect_args={'check_same_thread':False})
 Base.metadata.create_all(engine);sessions=sessionmaker(bind=engine)
 app=FastAPI();app.include_router(router,prefix='/api')
 def db():
  with sessions() as session:yield session
 app.dependency_overrides[get_db]=db
 return TestClient(app,base_url='https://workspace.example')

def test_workspace_session_is_dedicated_and_revoked_on_logout(tmp_path):
 c=make_client(tmp_path);r=c.post('/api/auth/register',json={'username':'tester','password':'offline-pass-123'});assert r.status_code==200
 token=c.cookies['ops_workspace_session'];assert decode_token(token)['token_type']=='workspace'
 cookies=r.headers.get_list('set-cookie');assert any('ops_workspace_session=' in x and 'HttpOnly' in x and 'Secure' in x and 'SameSite=strict' in x for x in cookies)
 h={'Authorization':'Bearer '+token};assert c.get('/api/auth/workspace/verify',headers=h).json()['username']=='tester'
 assert c.get('/api/auth/me',headers=h).status_code==401
 access={'Authorization':'Bearer '+r.json()['access_token']};assert c.get('/api/auth/workspace/verify',headers=access).status_code==401
 assert c.post('/api/auth/workspace/session',headers=access).status_code==200
 assert c.post('/api/auth/workspace/logout',headers=h).status_code==200
 assert c.get('/api/auth/workspace/verify',headers=h).status_code==401
 assert c.get('/api/auth/me',headers=access).status_code==401

def test_workspace_subject_follows_auth_user_not_request_fields(tmp_path):
 c=make_client(tmp_path);r=c.post('/api/auth/register',json={'username':'owner-one','password':'offline-pass-123'})
 old=c.cookies['ops_workspace_session'];r2=c.post('/api/auth/register',json={'username':'owner-two','password':'offline-pass-456'})
 response=c.post('/api/auth/workspace/session',json={'user_id':1},headers={'Authorization':'Bearer '+r2.json()['access_token']});assert response.json()['id']==2
 assert c.get('/api/auth/workspace/verify',headers={'Authorization':'Bearer '+old}).json()['id']==1
 assert c.get('/api/auth/workspace/verify',headers={'Authorization':'Bearer '+c.cookies['ops_workspace_session']}).json()['id']==2


def test_cross_port_exchange_requires_trusted_origin_and_uses_cookie_owner(tmp_path):
 c=make_client(tmp_path)
 a=c.post('/api/auth/register',json={'username':'owner-one','password':'offline-pass-123'}).json()
 b=c.post('/api/auth/register',json={'username':'owner-two','password':'offline-pass-456'}).json()
 path='/api/auth/workspace/exchange'
 assert c.post(path).status_code==403
 assert c.post(path,headers={'Origin':'https://evil.example','X-App-Request':'1'}).status_code==403
 for origin in ['https://d.58nft.net:9450','https://d.58nft.net:9461']:
  result=c.post(path,headers={'Origin':origin,'X-App-Request':'1','Authorization':'Bearer '+a['access_token']},json={'user_id':a['user']['id']})
  assert result.status_code==200
  assert result.json()['user']['id']==b['user']['id']
 assert c.get('/api/auth/me',headers={'Authorization':'Bearer '+a['access_token']}).status_code==401
 assert c.post('/api/auth/refresh',json={'refresh_token':a['refresh_token']}).status_code==401
 assert c.get('/api/auth/me',headers={'Authorization':'Bearer '+b['access_token']}).status_code==200
 c.post('/api/auth/logout',headers={'Authorization':'Bearer '+b['access_token']})
 assert c.post(path,headers={'Origin':'https://d.58nft.net:9461','X-App-Request':'1'}).status_code==401
