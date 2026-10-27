"""Business endpoint credentials and platform effects are entirely synthetic."""
import json,time
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from backend.app.core.database import Base
from backend.app.models import User,PlatformAccount,PublishJob,PublishAsset,AiDraft
from backend.app.models.direct_message import DirectMessage,DmListener
from backend.app.api import business
from backend.app.services.business_tasks import BusinessLedger
from backend.app.core.time import shanghai_now

@pytest.fixture
def env(tmp_path,monkeypatch):
    engine=create_engine('sqlite:///'+str(tmp_path/'db.sqlite'));Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    with factory() as db:
        db.add_all([User(id=1,username='own',password_hash='test'),User(id=2,username='other',password_hash='test')])
        db.add_all([PlatformAccount(id=1,user_id=1,platform='xhs',sub_type='pc',external_user_id='own',status='active'),
          PlatformAccount(id=2,user_id=1,platform='xhs',sub_type='pc',external_user_id='own2',status='active'),
          PlatformAccount(id=3,user_id=2,platform='xhs',sub_type='pc',external_user_id='other',status='active'),
          PlatformAccount(id=4,user_id=1,platform='xhs',sub_type='creator',external_user_id='own',status='active')])
        db.add(DmListener(account_id=1,enabled=True,auto_reply=True,route_id='r',control_mode='full_access',state='listening'))
        db.add(PublishJob(id=1,user_id=1,platform='xhs',platform_account_id=4,title='image title',body='synthetic',status='pending'))
        db.add(PublishAsset(id=1,publish_job_id=1,asset_type='image',file_path='/synthetic',upload_status='uploaded',creator_upload_info=json.dumps({'fileIds':['synthetic']})))
        db.commit()
    cfg=tmp_path/'credential.json';cfg.write_text(json.dumps({'token':'synthetic-private','owner_id':1,'account_ids':[1,2], 'operations':sorted(business.SUPPORTED)}))
    monkeypatch.setattr(business,'CONFIG',cfg);monkeypatch.setattr(business,'SessionLocal',factory)
    ledger=BusinessLedger(tmp_path/'ledger.sqlite');monkeypatch.setattr(business,'_ledger',ledger)
    monkeypatch.setattr(business,'_get_owned_pc_account_cookies',lambda *args:'synthetic')
    app=FastAPI();app.include_router(business.router,prefix='/api');client=TestClient(app);client.headers['Authorization']='Bearer synthetic-private'
    authority={'revision':1,'enabled':True,'expires_at':time.time()+30,'project_id':'project','grants':[{'account_id':1,'operations':sorted(business.SUPPORTED),'scope':{'resource_ids':['1','note'],'conversation_ids':['peer','comment'],'include_goal_created':True}}]}
    assert client.put('/api/business/goals/goal/authority',json=authority).status_code==200
    body={'request_id':'r','goal_id':'goal','project_id':'project','action_id':'a','account_id':1,'operation':'profile.read','input':{},'authorization_revision':1}
    return client,ledger,factory,body,authority

def test_auth_is_dedicated_and_account_isolated(env):
    c,s,f,b,a=env
    assert c.get('/api/business/capabilities',headers={'Authorization':'Bearer fake'}).status_code==401
    assert len(c.get('/api/business/capabilities').json()['resources'])==2
    assert c.post('/api/business/tasks',json={**b,'account_id':3}).status_code==409
    assert c.post('/api/business/tasks',json={**b,'account_id':2}).status_code==409

def test_profile_and_search_are_real_adapter_routes(env,monkeypatch):
    c,s,f,b,a=env
    calls=[]
    class Fake:
        def __init__(self,*args): pass
        def get_self_info(self):return {'user_id':'own','nickname':'synthetic','cookie':'secret'}
        def search_note(self,keyword,page):calls.append((keyword,page));return True,'',{'data':{'items':[],'has_more':True}}
    monkeypatch.setattr(business,'XhsPcApiAdapter',Fake)
    task=c.post('/api/business/tasks',json=b).json();s.tick(business.execute,business.check)
    result=c.get('/api/business/tasks/'+task['id']).json();assert result['status']=='succeeded' and 'cookie' not in json.dumps(result)
    task=c.post('/api/business/tasks',json={**b,'request_id':'search','operation':'public.search','input':{'keyword':'swimming','page':2}}).json();s.tick(business.execute,business.check)
    assert calls==[('swimming',2)] and s.get('machine:headquarters',task['id'])['result']['coverage']=='requested_public_search_page'

def test_publish_real_receipt_and_no_retry(env,monkeypatch):
    c,s,f,b,a=env
    from backend.app.api import publish
    from backend.app.adapters.xhs import creator_api_adapter
    monkeypatch.setattr(publish,'_get_latest_account_cookies',lambda *args:'synthetic')
    calls=[]
    class Fake:
        def __init__(self,*args): pass
        def post_note(self,info):calls.append(info);return {'data':{'note_id':'actual-test-id'}}
    monkeypatch.setattr(creator_api_adapter,'XhsCreatorApiAdapter',Fake)
    task=c.post('/api/business/tasks',json={**b,'operation':'content.publish','input':{'publish_job_id':1}}).json();s.tick(business.execute,business.check)
    result=s.get('machine:headquarters',task['id']);assert result['status']=='succeeded' and result['result']['source']=='authenticated_platform_submission'
    assert result['result']['data']['external_id']=='actual-test-id'
    other=c.post('/api/business/tasks',json={**b,'request_id':'duplicate','operation':'content.publish','input':{'publish_job_id':1}}).json();s.tick(business.execute,business.check)
    assert len(calls)==1

def test_timeout_publish_marks_job_uncertain(env,monkeypatch):
    c,s,f,b,a=env
    from backend.app.api import publish
    from backend.app.adapters.xhs import creator_api_adapter
    monkeypatch.setattr(publish,'_get_latest_account_cookies',lambda *args:'synthetic')
    class Fake:
        def __init__(self,*args):pass
        def post_note(self,info):raise TimeoutError()
    monkeypatch.setattr(creator_api_adapter,'XhsCreatorApiAdapter',Fake)
    task=c.post('/api/business/tasks',json={**b,'operation':'content.publish','input':{'publish_job_id':1}}).json();s.tick(business.execute,business.check)
    assert s.get('machine:headquarters',task['id'])['status']=='uncertain'
    with f() as db:assert db.get(PublishJob,1).status=='uncertain'

def test_goal_created_draft_publish_and_cross_goal_denied(env,monkeypatch):
    c,s,f,b,a=env
    task=c.post('/api/business/tasks',json={**b,'operation':'draft.create','input':{'title':'new','body':'synthetic','uploaded_asset_ids':[1]}}).json();s.tick(business.execute,business.check)
    result=s.get('machine:headquarters',task['id']);assert result['status']=='succeeded'
    job_id=result['result']['data']['publish_job_id'];assert job_id!=1
    submit={**b,'request_id':'new-publish','operation':'content.publish','input':{'publish_job_id':job_id}}
    from backend.app.api import publish
    from backend.app.adapters.xhs import creator_api_adapter
    monkeypatch.setattr(publish,'_get_latest_account_cookies',lambda *args:'synthetic')
    class Fake:
        def __init__(self,*args):pass
        def post_note(self,info):return {'data':{'note_id':'new-goal-note'}}
    monkeypatch.setattr(creator_api_adapter,'XhsCreatorApiAdapter',Fake)
    response=c.post('/api/business/tasks',json=submit); assert response.status_code==202
    s.tick(business.execute,business.check)
    published=s.get('machine:headquarters',response.json()['id'])
    assert published['status']=='succeeded' and published['result']['data']['external_id']=='new-goal-note'
    assert c.put('/api/business/goals/other/authority',json=a).status_code==200
    assert c.post('/api/business/tasks',json={**submit,'request_id':'cross-goal','goal_id':'other'}).status_code==409
    assert c.post('/api/business/tasks',json={**submit,'request_id':'cross-account','account_id':2}).status_code==409

def test_dm_model_intermediate_and_ack_completion(env):
    c,s,f,b,a=env
    with f() as db:
        db.add(DirectMessage(account_id=1,message_id='incoming',peer_id='peer',text='synthetic',channel_payload={'account_id':'user:1:account:own','message_id':'incoming'},receipt={},state='history_pending'));db.commit()
    task=c.post('/api/business/tasks',json={**b,'operation':'dm.reply','input':{'conversation_id':'peer','message_id':'incoming'}}).json();s.tick(business.execute,business.check)
    assert s.get('machine:headquarters',task['id'])['state']=='waiting_model'
    with f() as db:
        row=db.scalar(select(DirectMessage));assert row.channel_payload['message_id']=='business-r'
        row.state='ready';row.channel_event_id='channel-task';db.commit()
    business.reconcile_dm();assert s.get('machine:headquarters',task['id'])['state']=='waiting_platform'
    with f() as db:
        row=db.scalar(select(DirectMessage));row.state='sent';row.outbound_mid='mid';row.receipt={**row.receipt,'mid':'mid','code':0,'msg':'ok','messageid':'external'};db.commit()
    business.reconcile_dm();result=s.get('machine:headquarters',task['id']);assert result['state']=='succeeded' and result['result']['source']=='authenticated_platform_ack'

def test_dm_guard_revoke_before_claim(env):
    c,s,f,b,a=env
    with f() as db:
        db.add(DirectMessage(account_id=1,message_id='incoming',peer_id='peer',text='test',channel_payload={'account_id':'user:1:account:own'},receipt={},state='history_pending'));db.commit()
    task=c.post('/api/business/tasks',json={**b,'operation':'dm.reply','input':{'conversation_id':'peer','message_id':'incoming'}}).json();s.tick(business.execute,business.check)
    c.put('/api/business/goals/goal/authority',json={**a,'revision':2,'enabled':False})
    from backend.app.services.dm_service import claim_send
    with f() as db:
        row=db.scalar(select(DirectMessage));row.state='ready';row.channel_payload={**row.channel_payload,'context':{'history':{'complete':True,'until':shanghai_now().isoformat()}}};db.commit()
        assert claim_send(db,row.id) is None and row.state=='held'

def test_comment_authentic_incoming_receipt_and_unknown(env,monkeypatch):
    c,s,f,b,a=env;calls=[]
    mention={'type':'comment/received','comment_info':{'id':'comment','content':'request'},'item_info':{'id':'note','user_info':{'userid':'own'}},'user_info':{'userid':'peer'}}
    class Fake:
        def __init__(self,*args):pass
        def get_mentions(self):return {'message_list':[mention]}
        def post_comment(self,note,text,target):calls.append((note,text,target));return {'success':True,'data':{'comment':{'id':'actual-comment'}}}
    monkeypatch.setattr(business,'XhsPcApiAdapter',Fake)
    task=c.post('/api/business/tasks',json={**b,'operation':'comment.reply','input':{'note_id':'note','target_comment_id':'comment','text':'synthetic'}}).json();s.tick(business.execute,business.check)
    result=s.get('machine:headquarters',task['id']);assert result['status']=='succeeded' and result['result']['data']['external_id']=='actual-comment'
    c.post('/api/business/tasks',json={**b,'request_id':'duplicate-comment','operation':'comment.reply','input':{'note_id':'note','target_comment_id':'comment','text':'changed'}});s.tick(business.execute,business.check)
    assert len(calls)==1

def test_model_output_is_not_platform_delivery(env,monkeypatch):
    c,s,f,b,a=env
    from backend.app.services import model_routing
    class Fake:
        def catalog(self):return {}
        def send(self,**kwargs):assert kwargs['message_id']=='business-r';return {'id':'model-task'}
        def status(self,event,identity):return {'state':'succeeded','result':{'output':{'text':'synthetic model draft'}}}
    monkeypatch.setattr(model_routing,'channel_client',lambda:Fake())
    monkeypatch.setattr(model_routing,'resolve_agent',lambda *args:({'id':'route'},{'id':'agent','name':'synthetic'}))
    task=c.post('/api/business/tasks',json={**b,'operation':'content.analyze','input':{'agent_id':'agent','text':'test'}}).json();s.tick(business.execute,business.check)
    assert s.get('machine:headquarters',task['id'])['state']=='waiting_model'
    business.reconcile_models();result=s.get('machine:headquarters',task['id'])
    assert result['state']=='succeeded' and result['result']['source']=='model_intermediate' and 'not_platform_action' in result['result']['coverage']

def test_reply_control_not_changed_and_no_synthetic_incoming(env):
    c,s,f,b,a=env
    with f() as db:
        row=db.get(DmListener,1);row.enabled=False;row.auto_reply=False;db.commit()
    assert c.post('/api/business/tasks',json={**b,'operation':'dm.reply','input':{'conversation_id':'peer','message_id':'invented'}}).status_code==409
    with f() as db:assert not db.get(DmListener,1).enabled and not db.scalar(select(DirectMessage))
    # Revocation must remain possible even after an account goes inactive.
    with f() as db:db.get(PlatformAccount,1).status='expired';db.commit()
    assert c.put('/api/business/goals/goal/authority',json={**a,'enabled':False,'revision':2}).status_code==200

def test_future_live_inbound_outbox_exact_binding_and_revoke(env):
    c,s,f,b,a=env
    from backend.app.models.direct_message import DmHistory
    from datetime import timedelta
    task=c.post('/api/business/tasks',json={**b,'operation':'message.read','input':{'conversation_id':'peer'}}).json();s.tick(business.execute,business.check)
    with f() as db:
        db.add_all([DmHistory(account_id=1,peer_id='peer',message_id='new',direction='inbound',text='synthetic',source='live',sent_at=shanghai_now(),created_at=shanghai_now()+timedelta(seconds=1)),
          DmHistory(account_id=2,peer_id='peer',message_id='wrong-account',direction='inbound',text='other',source='live',sent_at=shanghai_now(),created_at=shanghai_now()+timedelta(seconds=1)),
          DmHistory(account_id=1,peer_id='peer',message_id='import',direction='inbound',text='old',source='history',sent_at=shanghai_now(),created_at=shanghai_now()+timedelta(seconds=1))]);db.commit()
    business.scan_inbound();business.scan_inbound()
    events=c.get('/api/business/events').json()['items'];linked=[e for e in events if e['kind']=='message.inbound']
    assert len(linked)==1 and linked[0]['payload']['message_id']=='new' and linked[0]['task_id']==task['id'] and linked[0]['account_id']==1
    c.put('/api/business/goals/goal/authority',json={**a,'revision':2,'enabled':False})
    with f() as db:
        db.add(DmHistory(account_id=1,peer_id='peer',message_id='after-revoke',direction='inbound',text='future',source='live',sent_at=shanghai_now(),created_at=shanghai_now()+timedelta(seconds=2)));db.commit()
    business.scan_inbound();assert len([e for e in c.get('/api/business/events').json()['items'] if e['kind']=='message.inbound'])==1


def test_dm_offline_directory_is_unavailable(env):
    c,s,f,b,a=env
    with f() as db: db.get(DmListener,1).state='reconnecting';db.commit()
    resource=c.get('/api/business/capabilities').json()['resources'][0]
    op=next(o for o in resource['operations'] if o['id']=='dm.reply')
    assert not op['available'] and 'WebSocket' in op['reason']
    assert c.post('/api/business/tasks',json={**b,'operation':'dm.reply','input':{'conversation_id':'peer','message_id':'incoming'}}).status_code==409

def test_material_read_exposes_ids_without_paths_or_upload_credentials(env):
    c,s,f,b,a=env
    task=c.post('/api/business/tasks',json={**b,'operation':'material.read','input':{'publish_job_id':1}}).json()
    s.tick(business.execute,business.check);result=s.get('machine:headquarters',task['id'])
    assert result['state']=='succeeded' and result['result']['data']['assets']==[{'asset_id':1,'asset_type':'image','upload_status':'uploaded'}]
    assert 'file_path' not in json.dumps(result) and 'fileIds' not in json.dumps(result)

def test_native_publish_cannot_override_business_write_claim(env,monkeypatch):
    c,s,f,b,a=env
    from backend.app.api import publish
    from fastapi import HTTPException
    monkeypatch.setattr(publish,'_get_latest_account_cookies',lambda *args:'synthetic')
    original=publish._asset_creator_upload_info
    def claimed_before_native(asset):
        with f() as other:other.get(PublishJob,1).status='publishing';other.commit()
        return original(asset)
    monkeypatch.setattr(publish,'_asset_creator_upload_info',claimed_before_native)
    class Fake:
        def __init__(self,*args):pass
        def post_note(self,info):pytest.fail('must not overwrite another in-flight write claim')
    with f() as db:
        with pytest.raises(HTTPException) as error:publish.publish_job_to_creator(1,db.get(User,1),db,Fake)
        assert error.value.status_code==400

@pytest.mark.parametrize('channel_state,output,expected',[
    ('succeeded',{'structured':{'status':'needs_context','summary':'Please provide information'},'text':'transport completed'},'failed'),
    ('succeeded',{'structured':{'status':'blocked','body':'not a usable draft'}},'failed'),
    ('succeeded',{'structured':{'status':'error','error':'synthetic'}},'failed'),
    ('succeeded',{},'failed'),
    ('succeeded',{'text':'  '},'failed'),
    ('succeeded',{'structured':{'status':'completed'}},'failed'),
    ('succeeded',{'text':'{"status":"needs_context","summary":"missing details"}'},'failed'),
    ('succeeded',{'structured':{'status':'completed','body':'synthetic usable draft'}},'succeeded'),
    ('completed',{'text':'synthetic demo draft'},'succeeded'),
])
def test_real_channel_completion_requires_business_artifact(env,monkeypatch,channel_state,output,expected):
    c,s,f,b,a=env
    from backend.app.services import model_routing
    class Fake:
        def catalog(self):return {}
        def send(self,**kwargs):return {'id':'real-protocol-event'}
        def status(self,event,identity):
            assert event=='real-protocol-event' and identity=='user:1:account:own'
            return {'id':event,'state':channel_state,'result':{'output':output}}
    monkeypatch.setattr(model_routing,'channel_client',lambda:Fake())
    monkeypatch.setattr(model_routing,'resolve_agent',lambda *args:({'id':'route'},{'id':'agent','name':'synthetic'}))
    task=c.post('/api/business/tasks',json={**b,'operation':'content.analyze','input':{'agent_id':'agent','text':'test'}}).json()
    s.tick(business.execute,business.check);assert s.get('machine:headquarters',task['id'])['state']=='waiting_model'
    business.reconcile_models(); result=s.get('machine:headquarters',task['id'])
    assert result['state']==expected
    assert result['result']['source']=='model_intermediate'
    assert result['result']['coverage']!='platform_receipt'

def test_initial_goal_revision_zero_authority_and_profile_submission(env,monkeypatch):
    c,s,f,b,a=env
    class Fake:
        def __init__(self,*args):pass
        def get_self_info(self):return {'user_id':'own','nickname':'synthetic initial goal'}
    monkeypatch.setattr(business,'XhsPcApiAdapter',Fake)
    initial={**a,'revision':0,'project_id':'initial-project'}
    assert c.put('/api/business/goals/initial-goal/authority',json=initial).status_code==200
    submit={**b,'goal_id':'initial-goal','project_id':'initial-project','authorization_revision':0}
    response=c.post('/api/business/tasks',json=submit);assert response.status_code==202
    s.tick(business.execute,business.check)
    result=c.get('/api/business/tasks/'+response.json()['id']).json()
    assert result['authorization_revision']==0 and result['status']=='succeeded'
    assert result['result']['source']=='authenticated_platform_sdk'

@pytest.mark.parametrize('revision',[-1,False,True,'0',0.5])
def test_revision_must_be_nonnegative_integer_not_boolean(env,revision):
    c,s,f,b,a=env
    assert c.put('/api/business/goals/new-goal/authority',json={**a,'revision':revision}).status_code==409
    assert c.post('/api/business/tasks',json={**b,'authorization_revision':revision}).status_code==422
