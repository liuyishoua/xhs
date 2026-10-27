"""Synthetic 906 -> SMS recovery -> publish, with no real platform effects."""
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base
from backend.app.core.security import encrypt_text
from backend.app.models import AccountCookieVersion, ApiLog, PlatformAccount, PublishAsset, PublishJob, User
from backend.app.api import publish, account_maintenance
from backend.app.services import publish_recovery
from backend.app.services.account_health import CreatorUploadError

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'account-maintenance'))
from maintenance.store import Store
from maintenance.worker import Worker
from maintenance.sms import receive


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'xhs.sqlite3'))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(User(id=1, username='test-owner', password_hash='unused'))
        db.add(PlatformAccount(id=4, user_id=1, platform='xhs', sub_type='creator', external_user_id='owned', status='active'))
        db.add(AccountCookieVersion(platform_account_id=4, encrypted_cookies=encrypt_text('a1=synthetic')))
        db.add(PublishJob(id=18, user_id=1, platform_account_id=4, platform='xhs', title='Synthetic', body='test', status='pending'))
        db.add(PublishAsset(publish_job_id=18, asset_type='image', file_path='synthetic.jpg'))
        db.commit()
    state = {'permission': False, 'posts': 0, 'timeout': False}
    class Adapter:
        def __init__(self, cookies): pass
        def upload_media(self, *args):
            if not state['permission']: raise CreatorUploadError({'business_code':906,'http_status':401})
            return {'fileIds':'synthetic','width':100,'height':100}
        def post_note(self, info):
            state['posts'] += 1
            if state['timeout']:raise TimeoutError('synthetic lost receipt')
            return {'note_id':'synthetic-note'}
    monkeypatch.setattr(publish_recovery, 'SessionLocal', factory)
    monkeypatch.setattr(publish_recovery, 'get_creator_publish_adapter_factory', lambda: Adapter)
    return factory, state, Adapter


def attempt(env):
    factory, _, adapter = env
    with factory() as db:
        with pytest.raises(Exception):
            publish.publish_job_to_creator(18, current_user=db.get(User,1), db=db, adapter_factory=adapter)
        assert db.get(PublishJob,18).status == 'waiting_login'


def test_entire_recovery_is_automatic_and_posts_once(env, tmp_path):
    factory, state, _ = env
    attempt(env)
    with factory() as db:
        events = account_maintenance.events(0, SimpleNamespace(config={'user_id':1,'event_after':0}), db)['items']
        assert len(events)==1 and events[0]['business_code']==906 and not events[0]['ignored']
    center=Store(tmp_path/'center')
    center.sync('xhs',[{'id':'4','external_id':'owned','name':'test','kind':'creator','status':'risk','capability':'sms','url':'https://example.org'}])
    center.bind('xhs','4','13800000000')
    event=events[0]
    id,_=center.submit_work_order('xhs',{**event,'schema_version':1,'ticket_id':event['event_id']})
    assert center.job(id)['state']=='queued'
    class Connector:
        starts=0
        def events(self,after):return []
        def status(self,id):return None
        def start(self,job,phone):
            self.starts+=1
            assert job['force_login']==1
            return {'state':'waiting','remote_id':'synthetic'}
        def confirm(self,job,phone,code):
            assert code=='654321'
            state['permission']=True
            with factory() as db:
                db.get(PlatformAccount,4).status='active'
                db.add(AccountCookieVersion(platform_account_id=4,encrypted_cookies=encrypt_text('a1=new-synthetic-login')))
                db.commit()
            return {'state':'confirmed'}
    connector=Connector();worker=Worker(center,{'xhs':connector});worker.tick()
    publish_recovery.run_once();assert state['posts']==0
    secret='synthetic-device'
    with center.db(True) as db:
        db.execute('INSERT INTO devices VALUES(?,?,?,?,?,?)',('test',center.encrypt(secret),'1',json.dumps({'xhs':[]}),0,''))
    stamp=str(int(time.time()*1000))
    signed={'timestamp':stamp,'sign':base64.b64encode(hmac.new(secret.encode(),(stamp+'\n'+secret).encode(),hashlib.sha256).digest()).decode(),'from':'1061','content':'【小红书】验证码：654321，3分钟有效。'}
    assert receive(center,{'xhs':connector},'test',signed)['state']=='confirmed'
    publish_recovery.run_once();publish_recovery.run_once();worker.tick()
    assert state['posts']==1 and connector.starts==1
    with factory() as db:
        assert db.get(PublishJob,18).status=='published'


@pytest.mark.parametrize('change', ['cancel', 'account', 'owner', 'timeout', 'permission'])
def test_resumption_respects_cancellation_identity_and_ambiguous_receipts(env, change):
    factory,state,_=env;attempt(env)
    with factory() as db:
        a=db.get(PlatformAccount,4);a.status='active'
        if change=='cancel':db.get(PublishJob,18).status='cancelled'
        if change=='account':a.external_user_id='replacement'
        if change=='owner':a.user_id=2
        state['permission']=change!='permission';state['timeout']=change=='timeout'
        db.commit()
    publish_recovery.run_once();publish_recovery.run_once()
    assert state['posts']==(1 if change=='timeout' else 0)
    with factory() as db:
        assert db.get(PublishJob,18).status==('cancelled' if change=='cancel' else 'uncertain' if change=='timeout' else 'failed')


def test_upload_only_does_not_authorize_publishing(env):
    factory,state,adapter=env
    with factory() as db:
        a=db.scalars(select(PublishAsset)).one()
        with pytest.raises(Exception):publish.upload_publish_asset(a.id,db.get(User,1),db,adapter)
        assert db.get(PublishJob,18).status=='pending'
        db.get(PlatformAccount,4).status='active';db.commit()
    state['permission']=True;publish_recovery.run_once();assert state['posts']==0


def test_restart_preserves_waiting_jobs_and_never_replays_inflight_posts(env):
    factory,state,_=env;attempt(env)
    publish_recovery.recover()
    with factory() as db:
        j=db.get(PublishJob,18);assert j.status=='waiting_login'
        j.status='publishing';db.commit()
    publish_recovery.recover();state['permission']=True;publish_recovery.run_once()
    with factory() as db:assert db.get(PublishJob,18).status=='uncertain'
    assert state['posts']==0


def test_native_issue_link_is_validated_and_reuses_original_job(env):
    from pydantic import ValidationError
    from backend.app.api.drafts import DraftSendToPublishRequest, send_draft_to_publish
    from backend.app.models import AiDraft
    factory, _, _ = env
    issue_id = '01a13d69-e643-70a1-93d8-d0b20b7fe2b2'
    with pytest.raises(ValidationError):
        DraftSendToPublishRequest(multica_issue_id='bad')
    with factory() as db:
        db.add(AiDraft(id=1, user_id=1, platform='xhs', title='Original', body='body'))
        db.commit()
        data = DraftSendToPublishRequest(platform_account_id=4, multica_issue_id=issue_id)
        first = send_draft_to_publish(1, data, db.get(User, 1), db)
        second = send_draft_to_publish(1, data, db.get(User, 1), db)
        assert first['id'] == second['id']
        assert first['publish_options']['multica_issue_id'] == issue_id
        db.get(PublishJob, first['id']).platform_account_id = None
        db.commit()
        with pytest.raises(Exception, match='409'):
            send_draft_to_publish(1, data, db.get(User, 1), db)


@pytest.mark.parametrize('status,note_id,expected', [
    ('waiting_login', '', 'blocked'), ('failed', '', 'blocked'),
    ('uncertain', '', 'blocked'), ('published', 'real-note', 'done'),
    ('published', '', None), ('pending', '', None),
])
def test_native_tracking_follows_actual_job_and_never_publishes(env, tmp_path, monkeypatch, status, note_id, expected):
    from backend.app.services import native_publish_tracking as tracking
    from backend.app.services.content_automation import Executor, Ledger
    factory, state, _ = env
    monkeypatch.setattr(tracking, 'SessionLocal', factory)
    issue_id = '01a13d69-e643-70a1-93d8-d0b20b7fe2b2'
    with factory() as db:
        job = db.get(PublishJob, 18)
        job.publish_options = json.dumps({'multica_issue_id': issue_id})
        job.status, job.external_note_id = status, note_id
        db.commit()
    ledger = Ledger(tmp_path / 'native.sqlite3')
    ledger.add('native:test', 'post', {}, issue_id)
    ledger.update('native:test', stage='rejected', detail='old JSON-only failure')
    calls = []
    remote = SimpleNamespace(issue=lambda _: {'status':'todo', 'assignee_id':'agent', 'assignee_type':'agent'},
        request=lambda method,path,body: calls.append((method, path, body)))
    executor = Executor({'user_id':1,'creator_account_id':4,'expected_external_user_id':'owned','agent_id':'agent'}, ledger, remote)
    tracking.reconcile(executor, ledger.entry('native:test'))
    row = ledger.entry('native:test')
    assert row['job_id'] == 18
    assert [c[2]['status'] for c in calls] == ([expected] if expected else [])
    assert all(c[2]['suppress_run'] for c in calls)
    assert row['stage'] == ('published' if expected == 'done' else 'native_waiting')
    assert state['posts'] == 0
    ledger.db.close()


def test_native_tracking_never_takes_another_issue_job_or_assignee(env, tmp_path, monkeypatch):
    from backend.app.services import native_publish_tracking as tracking
    from backend.app.services.content_automation import Executor, Ledger
    factory, state, _ = env
    monkeypatch.setattr(tracking, 'SessionLocal', factory)
    ledger = Ledger(tmp_path / 'native.sqlite3')
    ledger.add('test', 'post', {}, 'issue')
    remote = SimpleNamespace(issue=lambda _: {'status':'todo','assignee_id':'other','assignee_type':'agent'},
        request=lambda *a: pytest.fail('must not change another assignee'))
    executor=Executor({'user_id':1,'creator_account_id':4,'expected_external_user_id':'owned','agent_id':'agent'},ledger,remote)
    tracking.reconcile(executor,ledger.entry('test'))
    assert ledger.entry('test')['job_id'] is None
    with factory() as db:
        job=db.get(PublishJob,18);job.publish_options=json.dumps({'multica_issue_id':'issue'})
        job.status='published';job.external_note_id='real-note';db.commit()
    with pytest.raises(ValueError, match='负责人'):
        tracking.reconcile(executor,ledger.entry('test'))
    assert ledger.entry('test')['stage'] != 'published'
    assert state['posts']==0
    ledger.db.close()


def test_native_status_sync_retries_without_republishing(env, tmp_path, monkeypatch):
    from backend.app.services import native_publish_tracking as tracking
    from backend.app.services.content_automation import Executor, Ledger
    factory, state, _ = env
    monkeypatch.setattr(tracking, 'SessionLocal', factory)
    with factory() as db:
        job=db.get(PublishJob,18)
        job.publish_options=json.dumps({'multica_issue_id':'issue'})
        job.status='published';job.external_note_id='receipt';db.commit()
    ledger=Ledger(tmp_path/'native.sqlite3');ledger.add('test','post',{},'issue')
    calls=[]
    def request(method,path,body):
        calls.append(body)
        if len(calls)==1:raise TimeoutError('synthetic status response lost')
    remote=SimpleNamespace(issue=lambda _: {'status':'blocked','assignee_id':'agent','assignee_type':'agent'},request=request)
    executor=Executor({'user_id':1,'creator_account_id':4,'expected_external_user_id':'owned','agent_id':'agent'},ledger,remote)
    with pytest.raises(TimeoutError):tracking.reconcile(executor,ledger.entry('test'))
    assert ledger.entry('test')['stage']!='published'
    tracking.reconcile(executor,ledger.entry('test'))
    assert ledger.entry('test')['stage']=='published'
    assert len(calls)==2 and state['posts']==0
    ledger.db.close()


def test_automatic_final_artifact_job_waits_for_login_then_posts_once(env):
    factory,state,_=env
    with factory() as db:
        job=db.get(PublishJob,18)
        job.publish_options=json.dumps({'automatic_publish':True,'multica_issue_id':'issue'})
        db.commit()
    publish_recovery.run_once()
    with factory() as db:assert db.get(PublishJob,18).status=='waiting_login'
    assert state['posts']==0
    state['permission']=True
    with factory() as db:db.get(PlatformAccount,4).status='active';db.commit()
    publish_recovery.run_once();publish_recovery.run_once()
    assert state['posts']==1
    with factory() as db:assert db.get(PublishJob,18).status=='published'


def test_completed_agent_attachments_are_persisted_before_platform_checks(env,tmp_path,monkeypatch):
    from io import BytesIO
    from PIL import Image
    from backend.app.services import native_publish_artifacts as artifacts
    factory,state,_=env
    monkeypatch.setattr(artifacts,'SessionLocal',factory)
    monkeypatch.setattr(artifacts,'ROOT',tmp_path/'assets')
    iid='01a13d93-d3c3-754f-9050-dbfb2537c310'
    note={'id':'01a13d96-1ce9-7204-92aa-e787910159c3','filename':'note.md','uploader_type':'agent','uploader_id':'agent','comment_id':'final'}
    image={'id':'01a13d96-1dde-7458-a253-142bc5172037','filename':'image.png','content_type':'image/png','uploader_type':'agent','uploader_id':'agent','comment_id':'final'}
    buf=BytesIO();Image.new('RGB',(100,100),'red').save(buf,format='PNG')
    monkeypatch.setattr(artifacts,'download',lambda r,a: '标题\n\n已完成的正文'.encode() if a['id']==note['id'] else buf.getvalue())
    remote=SimpleNamespace(issue=lambda _: {'status':'blocked','assignee_type':'agent','assignee_id':'agent','attachments':[note,image]},
        runs=lambda _: [{'status':'completed','agent_id':'agent','created_at':'2026-10-15'}])
    executor=SimpleNamespace(remote=remote,config={'agent_id':'agent','user_id':1,'creator_account_id':4})
    job_id=artifacts.prepare(executor,{'issue_id':iid})
    assert artifacts.prepare(executor,{'issue_id':iid})==job_id
    with factory() as db:
        job=db.get(PublishJob,job_id)
        assert job.status=='pending' and job.title=='标题'
        assert json.loads(job.publish_options)['automatic_publish'] is True
        asset=db.scalars(select(PublishAsset).where(PublishAsset.publish_job_id==job_id)).one()
        assert Path(asset.file_path).is_file()
    assert state['posts']==0
    remote.issue=lambda _: {'status':'cancelled','assignee_type':'agent','assignee_id':'agent','attachments':[note,image]}
    assert artifacts.prepare(executor,{'issue_id':iid}) is None


def test_deleted_publish_job_is_never_recreated_from_attachments(env,tmp_path,monkeypatch):
    from backend.app.services import native_publish_tracking as tracking
    from backend.app.services.content_automation import Executor,Ledger
    factory,_,_=env;monkeypatch.setattr(tracking,'SessionLocal',factory)
    ledger=Ledger(tmp_path/'deleted.sqlite3');ledger.add('test','post',{},'issue');ledger.update('test',job_id=999)
    executor=Executor({'user_id':1,'creator_account_id':4,'expected_external_user_id':'owned','agent_id':'agent','native_artifact_intake':True},ledger,object())
    tracking.reconcile(executor,ledger.entry('test'))
    assert ledger.entry('test')['stage']=='stopped'
    ledger.db.close()
