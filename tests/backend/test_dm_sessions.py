"""Real Channel API and XHS adapter, synthetic Multica, no platform I/O."""
import importlib.util
import json
from pathlib import Path
import sys
from datetime import timedelta
import pytest
from sqlalchemy import create_engine, select, text
from alembic.migration import MigrationContext
from alembic.operations import Operations
from test_direct_messages import database, event, empty_history
from test_direct_messages import history_message
from backend.app.core.time import SHANGHAI_TZ, shanghai_now
from backend.app.models import PlatformAccount
from backend.app.models.direct_message import DmHistory, DmListener, DmConversation
from backend.app.services import dm_history, dm_service as dm
from backend.app.services.channel_client import ChannelClient, ChannelError
sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'channel'))
pytest.importorskip('flask', reason='跨项目测试需同时提供 Channel 的测试依赖')
from channel_service.app import create_app
from channel_service.demo import DemoMultica, AGENT_ID


@pytest.fixture
def session_client(tmp_path, monkeypatch):
    class SyntheticMultica(DemoMultica):
        def _run(self,*args,**kwargs):
            super()._run(*args,**kwargs)
            self.task_rows[args[0]][-1]['result']={'content':json.dumps({'decision':'reply','reply_text':'合成回复','requires_human':False,'style':'简短自然','expression_memory':'不堆表情'})}
    remote=SyntheticMultica();app=create_app(tmp_path/'channel',remote);store=app.extensions['channel']
    token=store.source('xhs')['token'];route=store.save_route(dict(name='私信',agent_id=AGENT_ID,batch_seconds=0,max_batch=1))
    flask=app.test_client();worker=app.extensions['worker']
    class Client(ChannelClient):
        source='xhs'
        def __init__(self): self.calls=[];self.drop_sync=False;self.drop_send=False
        def request(self,method,path,data=None,**unused):
            response=flask.open(path,method=method,json=data,headers={'Authorization':'Bearer '+token})
            if response.status_code>=400: raise ChannelError('synthetic HTTP '+str(response.status_code),status=response.status_code)
            self.calls.append((method,path,data));result=response.get_json()
            if path.endswith('/messages') and path.startswith('/v1/sessions/') and self.drop_sync:
                self.drop_sync=False;raise ChannelError('lost sync response')
            if path=='/v1/messages':
                for _ in range(4):
                    with store.db(True) as sql:sql.execute('UPDATE batches SET next_poll=0,lease_until=0')
                    batch=store.claim()
                    if batch:worker.process(batch)
                if self.drop_send: self.drop_send=False;raise ChannelError('lost submit response')
            return result
    client=Client();client.store=store;client.remote=remote;client.route=route
    monkeypatch.setattr(dm,'channel_client',lambda:client)
    return client


def prepare(db,client,mid='platform-id'):
    db.get(DmListener,1).route_id=client.route['id'];db.commit()
    incoming=event();incoming['chatMessage']['messageid']=mid
    row=dm.ingest(db,db.get(PlatformAccount,1),db.get(DmListener,1),incoming)
    dm.prepare_history(db,row,empty_history)
    return row


def test_empty_platform_record_does_not_suppress_inbound_and_legacy_ledger_is_corrected(database, session_client):
    client = session_client
    with database() as db:
        row = prepare(db, client)
        stamp = shanghai_now() - timedelta(seconds=2)
        inbound = db.get(DmHistory, row.receipt['history_row_id'])
        inbound.sent_at = stamp
        placeholder = dm_history.save_message(db, 1, row.peer_id, 'empty-record', 'outbound',
            '[暂不支持的消息类型：0]', stamp+timedelta(milliseconds=2), content_type=0)
        db.commit()
        dm.poll_channel(db, row, client)
        assert row.state == 'skipped' and '本人已回复' in row.error
        old_snapshot = dict(row.receipt['session_snapshot'])
        placeholder_id = placeholder.id
        raw = history_message('empty-record', 2, stamp+timedelta(milliseconds=2), sender='own', receiver='customer-a')
        raw['content'] = json.dumps({'content_type': 0, 'content': ''})
        incoming = history_message('platform-id', 1, stamp)
        fetch = lambda *a, **kw: {'result': {'success': True}, 'out_message_list': [raw, incoming]}
        dm_history.sync_history(db, db.get(PlatformAccount, 1), row.peer_id, fetch, refresh_existing=True)
        assert db.get(DmHistory, placeholder_id).source == 'placeholder'
        assert all(r.id != placeholder_id for r in dm_history.history_rows(db, 1, row.peer_id, stamp-timedelta(days=1), shanghai_now()))
        dm.reset_analysis(db, row)
        dm.prepare_history(db, row, empty_history)
        dm.poll_channel(db, row, client)
        assert row.state == 'ready' and row.receipt['session_snapshot']['eligible']
        with client.store.db() as sql:
            saved = sql.execute('SELECT body FROM session_messages WHERE session_id=? AND message_id=?',
                (old_snapshot['session_id'], 'xhs-history:'+str(placeholder_id))).fetchone()
            assert json.loads(saved['body'])['revoked']
            previous = sql.execute('SELECT body FROM session_snapshots WHERE id=?', (old_snapshot['id'],)).fetchone()
            assert not json.loads(previous['body'])['eligible']


def test_owner_conversation_goal_is_scoped_and_passed_to_reply_task(database, session_client):
    with database() as db:
        row = prepare(db, session_client)
        conversation = dm_history.conversation(db, 1, row.peer_id)
        conversation.session_state = {'reply_goal': {'status': 'active', 'instruction': '自然聊清对方是想交流跑步还是另有事情，不要盘问。'}}
        db.commit()
        dm.poll_channel(db, row, session_client)
        assert row.state == 'ready'
        sent = row.receipt['channel_request']
        assert '自然聊清' in sent['text']
        assert sent['context']['owner_goal']['instruction'].startswith('自然聊清')
        assert not dm_history.conversation(db, 2, row.peer_id).session_state.get('reply_goal')


def test_incremental_sync_bounded_context_independent_tasks(database,session_client):
    client=session_client
    with database() as db:
        for i in range(60):
            dm_history.save_message(db,1,'customer-a','history-'+str(i),'inbound' if i%2 else 'outbound',
                '日常聊天'+str(i),shanghai_now()-timedelta(minutes=100-i))
        db.commit();row=prepare(db,client);dm.poll_channel(db,row,client)
        assert row.state=='ready'
        first_request=row.receipt['channel_request'];assert 'messages' not in first_request['context']
        assert first_request['session_snapshot_id'];assert '合成测试咨询' not in first_request['text']
        snapshots=[data for _,path,data in client.calls if path.endswith('/snapshots')]
        assert snapshots[0]['window_hours']==24
        assert row.channel_payload['context']['history']['window_hours']==24
        assert len(row.channel_payload['context']['messages'])<=26
        sending=dm.claim_send(db,row.id);assert sending
        dm.finish_send(db,row.id,{'mid':sending.outbound_mid,'code':0,'msg':'ok','messageid':'ack1'})
        second=prepare(db,client,'second');dm.poll_channel(db,second,client);assert second.state=='ready'
        sync_calls=[data for _,path,data in client.calls if path.startswith('/v1/sessions/') and path.endswith('/messages')]
        assert len(sync_calls[0]['messages'])==61
        assert len(sync_calls[-1]['messages'])==2
        assert len(client.remote.issues)==2 and all(c[0]=='create' for c in client.remote.calls)
        assert second.channel_payload['context']['previous_analysis']['expression_memory']=='不堆表情'
        assert db.scalar(select(DmConversation)).session_state['id']


def test_lost_sync_response_reuses_durable_versions(database,session_client):
    client=session_client;client.drop_sync=True
    with database() as db:
        row=prepare(db,client)
        with pytest.raises(ChannelError):dm.poll_channel(db,row,client)
        ledger=db.scalar(select(DmConversation)).session_state['messages']
        assert len(ledger)==1 and not next(iter(ledger.values()))['acknowledged']
        dm.poll_channel(db,row,client);assert row.state=='ready'
        syncs=[data for _,path,data in client.calls if path.startswith('/v1/sessions/') and path.endswith('/messages')]
        assert syncs[0]['messages']==syncs[1]['messages']
        with client.store.db() as sql:assert sql.execute('SELECT count(*) FROM session_messages').fetchone()[0]==1


def test_lost_model_submission_keeps_exact_snapshot_request(database,session_client):
    client=session_client;client.drop_send=True
    with database() as db:
        row=prepare(db,client)
        with pytest.raises(ChannelError):dm.poll_channel(db,row,client)
        original=json.dumps(row.receipt['channel_request'],sort_keys=True)
        dm.poll_channel(db,row,client)
        assert row.state=='ready' and len(client.remote.issues)==1
        assert original==json.dumps(row.receipt['channel_request'],sort_keys=True)


def test_expired_inbound_is_archived_but_does_not_become_reply_job(database,session_client):
    with database() as db:
        incoming=event();incoming['chatMessage']['ts']=int((shanghai_now()-timedelta(hours=2)).replace(tzinfo=SHANGHAI_TZ).timestamp()*1000)
        assert dm.ingest(db,db.get(PlatformAccount,1),db.get(DmListener,1),incoming) is None
        assert db.scalar(select(DmHistory)).text=='合成测试咨询' and not session_client.remote.calls


def test_remote_new_fact_invalidates_unsent_proposal(database,session_client):
    client=session_client
    with database() as db:
        row=prepare(db,client);dm.poll_channel(db,row,client)
        session_id=row.receipt['session_snapshot']['session_id']
        client.sync_session(row.channel_payload['account_id'],session_id,[dict(message_id='new',direction='outbound',text='本人已在另一端回复',sent_at=shanghai_now().replace(tzinfo=SHANGHAI_TZ).isoformat())])
        assert dm.claim_send(db,row.id) is None
        assert row.state=='history_pending' and not row.channel_event_id


def test_additive_session_migration_preserves_existing_history(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'old.db'))
    path=Path(__file__).resolve().parents[2]/'backend/alembic/versions/95d032f5b136_add_channel_session_state.py'
    spec=importlib.util.spec_from_file_location('session_migration',path);migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE xhs_dm_conversations(id INTEGER PRIMARY KEY,analysis JSON NOT NULL)"))
        connection.execute(text("INSERT INTO xhs_dm_conversations VALUES(1,'{}')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade();migration.upgrade()
        assert connection.execute(text('SELECT analysis,session_state FROM xhs_dm_conversations')).one()==('{}','{}')
    engine.dispose()
