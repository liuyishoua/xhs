import io,json
import urllib.error
from types import SimpleNamespace
import pytest
from fastapi import FastAPI,HTTPException,Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.app.core.database import Base
from backend.app.core import ops_identity
from backend.app.core.deps import get_current_user
from backend.app.api.identity_proxy import router


def test_remote_authority_selects_local_user_and_never_uses_local_password(tmp_path,monkeypatch):
 engine=create_engine('sqlite:///'+str(tmp_path/'business.db'));Base.metadata.create_all(engine)
 monkeypatch.setenv('OPS_IDENTITY_URL','http://identity.test')
 monkeypatch.setattr(ops_identity,'validate',lambda token,cookie:{'id':73,'username':'external-operator','auth_version':9})
 request=Request({'type':'http','method':'GET','path':'/api/accounts','headers':[]})
 with sessionmaker(bind=engine)() as db:
  user=get_current_user(request,'remote-access',db)
  assert user.id==73 and user.password_hash.startswith('external-only:')
  assert get_current_user(request,'remote-access',db).id==73
  other=ops_identity.local_user(db,{'id':74,'username':'second-operator','auth_version':0})
  assert other.id!=user.id
  with pytest.raises(HTTPException) as error:ops_identity.local_user(db,{'id':73,'username':'forged-name','auth_version':0})
  assert error.value.status_code==409


def test_identity_outage_fails_closed(monkeypatch):
 monkeypatch.setenv('OPS_IDENTITY_URL','http://identity.test')
 def fail(*args,**kwargs):raise OSError('offline')
 monkeypatch.setattr(ops_identity.urllib.request,'build_opener',lambda *args:SimpleNamespace(open=fail))
 with pytest.raises(HTTPException) as error:ops_identity.validate('old-local-token')
 assert error.value.status_code==503


def test_proxy_has_no_local_auth_fallback(monkeypatch):
 import httpx
 monkeypatch.setenv('OPS_IDENTITY_URL','http://identity.test')
 async def fail(*args,**kwargs):raise httpx.ConnectError('offline')
 monkeypatch.setattr(httpx.AsyncClient,'request',fail)
 app=FastAPI();app.include_router(router,prefix='/api');c=TestClient(app)
 assert c.post('/api/auth/login',json={'username':'old-user','password':'old-password'}).status_code==503
 assert c.post('/api/auth/not-a-route').status_code==404
