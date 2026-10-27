"""Manual chat uses synthetic sockets; never send to real platform contacts."""
import asyncio, io
from pathlib import Path
import importlib.util
import pytest
from PIL import Image
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, create_engine, text
from alembic.migration import MigrationContext
from alembic.operations import Operations
from test_direct_messages import database, accept, event, FakeChannel
from backend.app.api import dm_chat as api
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models import User, PlatformAccount
from backend.app.models.direct_message import DmListener, DmManualMessage, DmHistory, DirectMessage
from backend.app.services import dm_chat as chat, dm_worker, dm_service as dm, dm_media

@pytest.fixture
def client(database,monkeypatch,tmp_path):
    monkeypatch.setattr(dm_worker,'SessionLocal',database)
    monkeypatch.setattr(dm_media,'ROOT',tmp_path/'images')
    app=FastAPI();app.include_router(api.router,prefix='/api')
    def db():
        with database() as session:yield session
    app.dependency_overrides[get_db]=db
    with database() as session:owner=session.get(User,1)
    app.dependency_overrides[get_current_user]=lambda:owner
    return TestClient(app)

def payload(client_id='first',peer='customer-a',**changes):
    return {'client_id':client_id,'peer_id':peer,'expected_external_user_id':'own','text':'手动合成测试',**changes}

def test_manual_send_is_owned_idempotent_and_does_not_take_over(client,database):
    with database() as db:row=accept(db);old_id=row.id
    first=client.post('/api/xhs/chat/accounts/1/send',json=payload())
    assert first.status_code==200 and first.json()['state']=='queued'
    assert client.post('/api/xhs/chat/accounts/1/send',json=payload()).json()['id']==first.json()['id']
    assert client.post('/api/xhs/chat/accounts/1/send',json=payload(text='changed')).status_code==409
    assert client.post('/api/xhs/chat/accounts/2/send',json=payload()).status_code==404
    assert client.post('/api/xhs/chat/accounts/1/send',json=payload(expected_external_user_id='other')).status_code==409
    with database() as db:
        assert len(list(db.scalars(select(DmManualMessage))))==1
        assert db.get(DirectMessage,old_id).state=='skipped'
        assert not chat.controlled(db,1,'customer-a') and not chat.controlled(db,1,'different-peer')
        listener=db.get(DmListener,1);assert listener.enabled and listener.auto_reply and listener.manual_chat_enabled

def test_new_inbound_after_manual_send_still_enters_auto_reply(client,database):
    result=client.post('/api/xhs/chat/accounts/1/send',json=payload()).json()
    with database() as db:
        row=chat.claim(db,result['id'],'own')
        chat.finish(db,row.id,{'mid':row.outbound_mid,'code':0,'messageid':'manual-ack'})
    dm_worker.receive(1,event(text='新的问题'))
    with database() as db:
        row=db.scalars(select(DirectMessage)).one()
        assert row.text=='新的问题' and row.state not in ('skipped','sent')

def test_explicit_takeover_is_preserved_when_sending(client,database):
    assert client.post('/api/xhs/chat/accounts/1/control',json={'peer_id':'customer-a','enabled':True}).status_code==200
    assert client.post('/api/xhs/chat/accounts/1/send',json=payload()).status_code==200
    with database() as db:
        assert chat.controlled(db,1,'customer-a')
        assert not chat.controlled(db,1,'different-peer')

def test_invalid_and_other_account_images_cannot_be_sent(client,database):
    assert client.post('/api/xhs/chat/accounts/1/send',json=payload(text='')).status_code==400
    assert client.post('/api/xhs/chat/accounts/1/send',json=payload(text='x'*2001)).status_code==422
    assert client.post('/api/xhs/chat/accounts/1/send',json=payload(text='',asset_id='missing')).status_code==404
    assert client.post('/api/xhs/chat/accounts/1/send',json=payload(peer='own')).status_code==400
    assert client.get('/api/xhs/chat/messages?account_id=2&peer_id=customer-a').status_code==404

def test_existing_automatic_send_blocks_manual_enqueue(client,database):
    with database() as db:row=accept(db);row.state='sending';db.commit()
    assert client.post('/api/xhs/chat/accounts/1/send',json=payload()).status_code==409
    with database() as db:assert not list(db.scalars(select(DmManualMessage)))

def test_manual_socket_send_records_ack_and_deduplicates_history(client,database,monkeypatch):
    result=client.post('/api/xhs/chat/accounts/1/send',json=payload()).json();pending={};calls=[]
    monkeypatch.setattr(dm_worker.xhs_network,'require_ready',lambda:None)
    class Socket:
        async def send(self,peer,body,mid):
            calls.append((peer,body))
            pending[mid].set_result({'mid':mid,'code':0,'msg':'ok','messageid':'manual-native-id'})
    assert asyncio.run(dm_worker.deliver_manual(1,Socket(),pending,'own'))
    assert not asyncio.run(dm_worker.deliver_manual(1,Socket(),pending,'own'))
    assert calls==[('customer-a','手动合成测试')]
    rows=client.get('/api/xhs/chat/messages?account_id=1&peer_id=customer-a').json()
    assert len(rows['items'])==1 and rows['items'][0]['state']=='sent'
    with database() as db:assert db.get(DmManualMessage,result['id']).state=='sent'
    with database() as db:
        saved=db.scalars(select(DmHistory)).one();saved.message_id='decorated-history-id';db.commit()
    assert len(client.get('/api/xhs/chat/messages?account_id=1&peer_id=customer-a').json()['items'])==1

def test_uncertain_send_blocks_only_same_peer_and_requires_resolution(client,database):
    first=client.post('/api/xhs/chat/accounts/1/send',json=payload()).json()
    with database() as db:
        row=chat.claim(db,first['id'],'own');assert row
        chat.finish(db,row.id,None)
    second=client.post('/api/xhs/chat/accounts/1/send',json=payload(client_id='second')).json()
    other=client.post('/api/xhs/chat/accounts/1/send',json=payload(client_id='third',peer='other-peer')).json()
    with database() as db:assert chat.next_message(db,1).id==other['id']
    assert client.post('/api/xhs/chat/accounts/1/control',json={'peer_id':'customer-a','enabled':False}).status_code==409
    assert client.post(f"/api/xhs/chat/requests/{first['id']}/resolve",json={'outcome':'skip'}).status_code==200
    with database() as db:assert chat.next_message(db,1).id==second['id']
    assert client.post(f"/api/xhs/chat/requests/{second['id']}/resolve",json={'outcome':'confirmed_sent'}).status_code==409

def test_wrong_account_connection_cannot_claim_queued_message(client,database):
    row=client.post('/api/xhs/chat/accounts/1/send',json=payload()).json()
    with database() as db:
        assert chat.claim(db,row['id'],'different-account') is None
        assert db.get(DmManualMessage,row['id']).state=='failed'

def test_manual_only_connection_archives_inbound_without_model_jobs(client,database):
    with database() as db:
        listener=db.get(DmListener,1);listener.enabled=False;db.commit()
    assert client.post('/api/xhs/chat/accounts/1/connect',json={'enabled':True}).status_code==200
    assert 1 in dm_worker.enabled_accounts()
    dm_worker.receive(1,event())
    with database() as db:
        assert list(db.scalars(select(DmHistory)))
        assert not list(db.scalars(select(DirectMessage)))
    client.post('/api/xhs/chat/accounts/1/connect',json={'enabled':False})
    assert 1 not in dm_worker.enabled_accounts()

def test_manual_takeover_blocks_stale_generated_reply(client,database,monkeypatch):
    monkeypatch.setattr(dm,'channel_client',FakeChannel)
    with database() as db:
        row=accept(db);dm.poll_channel(db,row,FakeChannel());row_id=row.id
    client.post('/api/xhs/chat/accounts/1/control',json={'peer_id':'customer-a','enabled':True})
    with database() as db:
        row=db.get(DirectMessage,row_id);row.state='ready';db.commit()
        assert dm.claim_send(db,row_id) is None
    dm_worker.receive(1,{**event(),'chatMessage':{**event()['chatMessage'],'messageid':'new-incoming'}})
    with database() as db:assert len(list(db.scalars(select(DirectMessage))))==1

def test_restart_does_not_resend_inflight_manual_message(client,database):
    row=client.post('/api/xhs/chat/accounts/1/send',json=payload()).json()
    with database() as db:assert chat.claim(db,row['id'],'own')
    dm_worker.recover()
    with database() as db:
        assert db.get(DmManualMessage,row['id']).state=='uncertain'
        assert chat.next_message(db,1) is None

def test_image_send_uses_account_owned_local_asset(client,database,monkeypatch):
    output=io.BytesIO();Image.new('RGB',(2,2),'red').save(output,format='PNG');blob=output.getvalue()
    image=client.post('/api/xhs/chat/accounts/1/images',files={'file':('test.png',blob,'image/png')})
    assert image.status_code==200;asset=image.json()['asset_id']
    assert client.post('/api/xhs/chat/accounts/1/send',json=payload(text='',asset_id=asset)).status_code==200
    pending={};calls=[];monkeypatch.setattr(dm_worker.xhs_network,'require_ready',lambda:None)
    class Socket:
        def upload_image(self,path):
            assert path.read_bytes()==blob
            return {'url':'https://sns-webpic-qc.xhscdn.com/test.png','file_id':'synthetic','width':2,'height':2}
        async def send(self,peer,body,mid,reply):
            calls.append(reply);pending[mid].set_result({'mid':mid,'code':0,'msg':'ok','messageid':'image-ack'})
    assert asyncio.run(dm_worker.deliver_manual(1,Socket(),pending,'own'))
    assert calls[0]['content_type']==2 and 'test.png' in calls[0]['content']
    assert client.get('/api/xhs/chat/messages?account_id=1&peer_id=customer-a').json()['items'][0]['media'][0]['asset_id']==asset

def test_migration_preserves_existing_listener_and_is_repeatable(tmp_path):
    module_path=Path(__file__).resolve().parents[2]/'backend/alembic/versions/b7a318c49d21_add_manual_dm_chat.py'
    spec=importlib.util.spec_from_file_location('manual_migration',module_path);migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    engine=create_engine('sqlite:///'+str(tmp_path/'migration.db'))
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE xhs_dm_listeners(account_id INTEGER PRIMARY KEY,enabled BOOLEAN)'))
        connection.execute(text('INSERT INTO xhs_dm_listeners VALUES(1,1)'))
        migration.op=Operations(MigrationContext.configure(connection));migration.upgrade();migration.upgrade()
        assert tuple(connection.execute(text('SELECT enabled,manual_chat_enabled,chat_refresh_requested FROM xhs_dm_listeners')).one())==(1,0,0)
    engine.dispose()
