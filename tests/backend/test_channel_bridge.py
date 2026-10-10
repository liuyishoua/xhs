from io import BytesIO
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from backend.app.api import channel
from backend.app.core.deps import get_current_user
from backend.app.core.database import get_db

@pytest.fixture
def bridge(monkeypatch):
    calls=[]
    class Client:
        def __init__(self,*args):pass
        def send(self,*args,**kwargs):calls.append(('send',args,kwargs));return {'id':'event','state':'queued'}
        def status(self,*args):calls.append(('status',args));return {'id':'event','state':'waiting'}
        def upload(self,*args):calls.append(('upload',args));return {'id':'asset'}
    class Database:
        def get(self,kind,account_id):return SimpleNamespace(user_id=1 if account_id==10 else 2,external_user_id='platform-id',id=account_id)
    monkeypatch.setattr(channel,'ChannelClient',Client)
    app=FastAPI();app.include_router(channel.router,prefix='/api');app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(id=1);app.dependency_overrides[get_db]=lambda:Database()
    return TestClient(app),calls

def test_channel_uses_workspace_identity_without_platform_account(bridge):
    client,calls=bridge;r=client.post('/api/channel/messages',json={'message_id':'stable-message','text':'分析','context':{'topic_id':'draft:1'}})
    assert r.status_code==202
    assert calls[0][1][0]=='user:1'
    assert calls[0][2]=={'surface':'content','event_type':'content.rewrite_requested'}

def test_channel_uses_owned_platform_identity_and_rejects_other_user(bridge):
    client,calls=bridge
    assert client.post('/api/channel/messages',json={'message_id':'stable-message','text':'分析','account_id':10}).status_code==202
    assert calls[0][1][0]=='user:1:account:platform-id'
    assert client.post('/api/channel/messages',json={'message_id':'other-message','text':'分析','account_id':20}).status_code==404
    assert len(calls)==1

def test_channel_result_is_scoped_to_current_user(bridge):
    client,calls=bridge
    assert client.get('/api/channel/messages/event').status_code==200
    assert calls[0][1]==('event','user:1')
    assert client.get('/api/channel/messages/event?account_id=20').status_code==404

def test_channel_image_upload_uses_same_account_scope(bridge):
    client,calls=bridge;r=client.post('/api/channel/assets',files={'file':('test.png',BytesIO(b'fixture'),'image/png')})
    assert r.status_code==200
    assert calls[0][1][0]=='user:1'

def test_channel_routes_require_existing_workspace_authentication():
    app=FastAPI();app.include_router(channel.router,prefix='/api');client=TestClient(app)
    assert client.post('/api/channel/messages',json={'message_id':'stable-message','text':'分析'}).status_code==401
