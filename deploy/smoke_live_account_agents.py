"""Real deployed Channel/Multica, synthetic workbench data and fake social effects only."""
from pathlib import Path
from datetime import datetime, timedelta
import json
import os
import re
import time
import uuid
from unittest.mock import patch
import requests
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from backend.app.core.database import Base, SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.core.security import create_access_token
from backend.app.models import PlatformAccount, User
from backend.app.models.direct_message import DmListener, DirectMessage, DmHistory
from backend.app.services import dm_service, dm_sessions
from backend.app.services.model_routing import channel_client, account_default, ensure_account_config, catalog_agents
from backend.app.services.content_automation import Executor, Ledger, Multica, PRIVATE, ROOT

BOUNDARY = '\n本轮是无副作用的隔离验收，全部对话、笔记、评论及收件人都是虚构测试数据。只完成当前JSON决策，不执行任何平台操作、不创建其他任务、不写账号长期记忆。读取你已挂载且适用的技能后处理即可。'


def main():
    marker = 'synthetic-agent-smoke-' + uuid.uuid4().hex[:12]
    folder = PRIVATE / ('live-agent-smoke-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    folder.mkdir(mode=0o700)
    def save(name,data):
        file = folder / name
        file.write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str)+'\n');file.chmod(0o600)
    live = channel_client()
    remote = Multica(ROOT / 'channel/.local/multica.json')
    catalog = live.catalog()
    with SessionLocal() as production:
        accounts = [dict(id=a.id,user_id=a.user_id,external_user_id=a.external_user_id,nickname=a.nickname)
                    for a in production.scalars(select(PlatformAccount).where(PlatformAccount.platform=='xhs',PlatformAccount.sub_type=='pc')).all()]
        live_listener_before = [dict(account_id=r.account_id,enabled=r.enabled,auto_reply=r.auto_reply,route_id=r.route_id,agent_snapshot=r.agent_snapshot)
                               for r in production.scalars(select(DmListener))]
    headers={'Authorization':'Bearer '+create_access_token(accounts[0]['user_id'])}
    http=requests.Session();http.trust_env=False
    online=[]
    for account in accounts:
        entry={'account':account['nickname']}
        for label,path in [('agents','/xhs/direct-messages/agents'),('routing','/model-routing'),('comments','/xhs/comments/monitoring')]:
            response=http.get('http://127.0.0.1:8772/api'+path,params={'account_id':account['id']},headers=headers,timeout=30)
            response.raise_for_status();body=response.json()
            if label=='comments': body={k:body.get(k) for k in ('account_id','enabled','auto_reply','agent_id','agent_snapshot','route_id')}
            entry[label]=body
        online.append(entry)
    save('online-bindings.json',{'accounts':online,'listeners_before':live_listener_before,'catalog_agents':catalog_agents(catalog)})
    engine=create_engine('sqlite:///'+str(folder/'isolated-workbench.sqlite3'))
    Base.metadata.create_all(engine);sessions=sessionmaker(bind=engine,expire_on_commit=False)
    sent=[];jobs=[];social_effects=[]
    class SafeChannel:
        def __getattr__(self,name):return getattr(live,name)
        def send(self,*args,**kwargs):
            # Actual payloads are frozen by Channel; marker is retained in every submitted test.
            kwargs['text']=kwargs.get('text','')+BOUNDARY
            kwargs['context']={**kwargs.get('context',{}),'synthetic_smoke':True,'test_marker':marker,'social_effects':'fake-only','long_term_memory_writes':False}
            kwargs['new_task']=not bool(kwargs.get('session_snapshot_id'))
            result=live.send(*args,**kwargs)
            sent.append({'id':result['id'],'account_id':str(args[0] if args else kwargs['account_id']), 'message_id':kwargs.get('message_id'), 'new_task':kwargs['new_task'], 'independent_snapshot':bool(kwargs.get('session_snapshot_id'))})
            save('submitted-events.json',sent)
            return result
    channel=SafeChannel()
    class FakeSocial:
        def post_comment(self,note_id,text,comment_id):
            assert marker in note_id and marker in comment_id
            cid=marker+'-fake-comment-ack-'+uuid.uuid4().hex[:8]
            social_effects.append({'type':'comment','target':comment_id,'text':text,'ack':cid,'fake':True})
            return {'data':{'comment':{'id':cid,'status':0}},'msg':'synthetic fake adapter'}
    class IsolatedExecutor(Executor):
        def pc(self):return FakeSocial()
        def verify_identity(self):return None
        def notify(self,*args):raise AssertionError('Test must not write real workbench notifications')
    with sessions() as db:
        db.add(User(id=accounts[0]['user_id'],username='synthetic-smoke-owner',password_hash='unused'))
        for identity in accounts:
            account=PlatformAccount(**identity,platform='xhs',sub_type='pc',status='active');db.add(account);db.flush()
            configs={}
            for purpose in ('text','image','dm','comment','content'):
                config=ensure_account_config(db,account,purpose,catalog)
                configs[purpose]={'account_binding':config.base_url,'route_id':config.model_name,'agent_snapshot':getattr(config,'_agent_snapshot',{})}
            save(account.nickname+'-defaults.json',configs)
            route,snapshot=account_default(catalog,'dm',account)
            listener=DmListener(account_id=account.id,enabled=True,auto_reply=True,control_mode='auto_approve',route_id=route['id'],agent_snapshot=snapshot,started_at=shanghai_now()-timedelta(minutes=1));db.add(listener);db.commit()
            peer=marker+'-peer-'+str(account.id)
            incoming={'chatMessage':{'messageid':marker+'-dm-'+str(account.id),'mid':marker+'-transport-'+str(account.id),
                'payload_json':{'sender':peer,'receiver':account.external_user_id,'content':'这是虚构验收聊天：下班想去公园走十分钟，你觉得带瓶水就够了吗？','contentType':1,'groupChat':False}}}
            row=dm_service.ingest(db,account,listener,incoming)
            dm_service.prepare_history(db,row,lambda *_args,**_kwargs:{'result':{'success':True},'out_message_list':[]})
            assert dm_sessions.prepare(db,row,channel)
            dm_service.poll_channel(db,row,channel)
            jobs.append({'kind':'dm','account':account.nickname,'account_id':account.id,'row_id':row.id,'event_id':row.channel_event_id,'status_account':row.channel_payload['account_id'],'agent_snapshot':snapshot})
            print(account.nickname+' 私信已提交真实 Channel',flush=True)
            route,snapshot=account_default(catalog,'comment',account)
            config={'user_id':account.user_id,'pc_account_id':account.id,'expected_external_user_id':account.external_user_id,
                'account_nickname':account.nickname,'comment_only':True,'route_id':route['id'],'agent_id':snapshot['id'],'agent_snapshot':snapshot}
            ledger=Ledger(folder/('isolated-comments-'+str(account.id)+'.sqlite3'))
            from backend.app.services.content_automation import comment_settings_key
            ledger.set_state(comment_settings_key(config),{'enabled':True,'auto_reply':True,'control_mode':'auto_approve','route_id':route['id'],'agent_id':snapshot['id'],'agent_snapshot':snapshot})
            key='comment:'+str(account.id)+':'+marker+'-comment'
            ledger.add(key,'comment',{'text':'公园散步要准备什么？','target_text':'虚构验收笔记：傍晚到公园走十分钟。','note_title':'虚构公园散步验收','kind':'comment',
                'note_id':marker+'-note-'+str(account.id),'comment_id':marker+'-comment-'+str(account.id),'peer_id':peer})
            executor=IsolatedExecutor(config=config,ledger=ledger,remote=remote,channel=channel)
            executor.process_comment(ledger.entry(key))
            entry=ledger.entry(key)
            jobs.append({'kind':'comment','account':account.nickname,'account_id':account.id,'key':key,'event_id':entry['event_id'],'status_account':str(account.id),'agent_snapshot':snapshot,'executor':executor})
            print(account.nickname+' 评论已提交真实 Channel',flush=True)
        save('jobs.json',[{k:v for k,v in j.items() if k!='executor'} for j in jobs])
        deadline=time.monotonic()+720
        completed=set()
        while time.monotonic()<deadline and len(completed)<len(jobs):
            for index,job in enumerate(jobs):
                if index in completed:continue
                status=live.status(job['event_id'],job['status_account'])
                if status['state'] not in ('succeeded','failed','failed_preflight','cancelled','uncertain'):continue
                job['channel_state']=status['state']
                save(job['account']+'-'+job['kind']+'-channel-history.json',status)
                if job['kind']=='dm':
                    row=db.get(DirectMessage,job['row_id']);dm_service.poll_channel(db,row,channel)
                    if row.state=='ready':
                        for _ in range(4):
                            sending=dm_service.claim_send(db,row.id)
                            if not sending:
                                # Only fake local scheduling is bypassed; real platform send is absent.
                                row.receipt={**row.receipt,'reply_due_at':shanghai_now().isoformat()};db.commit();sending=dm_service.claim_send(db,row.id)
                            if not sending:break
                            fake_mid=marker+'-fake-dm-ack-'+uuid.uuid4().hex[:8]
                            social_effects.append({'type':'dm','target':row.peer_id,'text':row.reply_text,'ack':fake_mid,'fake':True})
                            dm_service.finish_send(db,row.id,{'mid':row.outbound_mid,'messageid':fake_mid,'code':0,'msg':'ok'})
                            if row.state!='ready':break
                    job['workbench_state']=row.state
                    save(job['account']+'-dm-workbench-history.json',dm_service.message_view(row))
                else:
                    executor=job['executor'];executor.process_comment(executor.ledger.entry(job['key']))
                    if executor.ledger.entry(job['key'])['stage']=='ready':
                        with patch('backend.app.services.content_automation.xhs_network.check',return_value={'state':'online','label':'synthetic fake-only'}), patch('backend.app.services.content_automation.xhs_network.require_ready',return_value=None):
                            executor.process_comment(executor.ledger.entry(job['key']))
                    entry=dict(executor.ledger.entry(job['key']));job['workbench_state']=entry['stage']
                    save(job['account']+'-comment-workbench-history.json',entry)
                execution=status.get('execution') or {};job['issue_id']=execution.get('issue_id');job['task_id']=execution.get('task_id')
                if job['issue_id']:
                    runs=remote.runs(job['issue_id']);matched=[r for r in runs if r['id']==job['task_id']]
                    job['run_status']=matched[0].get('status') if matched else None
                    job['actual_agent_id']=matched[0].get('agent_id') if matched else None
                    save(job['account']+'-'+job['kind']+'-runs.json',runs)
                    transcript=remote.transcript(job['task_id']) if job['task_id'] else []
                    save(job['account']+'-'+job['kind']+'-transcript.json',transcript)
                    serialized=json.dumps(transcript,ensure_ascii=False)
                    job['skill_mentions']=sorted(set(re.findall(r'(?:social-[a-z-]+|platform-xhs|xhs-[a-z-]+|persona-[a-z-]+)',serialized)))
                    job['skill_file_reads']=serialized.count('SKILL.md')
                    detail=remote.request('GET','/api/agents/'+job['actual_agent_id']) if job['actual_agent_id'] else {}
                    save(job['account']+'-agent-detail.json',detail)
                completed.add(index)
                print(job['account']+' '+job['kind']+' Channel='+str(job['channel_state'])+' 隔离工作台='+str(job['workbench_state']),flush=True)
            save('progress.json',[{k:v for k,v in j.items() if k!='executor'} for j in jobs])
            if len(completed)<len(jobs):time.sleep(5)
    with SessionLocal() as production:
        assert production.scalar(select(DirectMessage.id).where(DirectMessage.message_id.like(marker+'%')).limit(1)) is None
        listeners_after=[dict(account_id=r.account_id,enabled=r.enabled,auto_reply=r.auto_reply,route_id=r.route_id,agent_snapshot=r.agent_snapshot) for r in production.scalars(select(DmListener))]
        assert listeners_after==live_listener_before,'Production listener config changed while smoke ran'
    for file in folder.glob('*'):
        if file.is_file():file.chmod(0o600)
    save('social-fake-receipts.json',social_effects)
    summary={'marker':marker,'deployed_live_channel':True,'deployed_real_multica':True,'isolated_workbench':True,'real_social_effects':False,
        'production_listener_unchanged':True,'production_synthetic_rows':0,'jobs':[{k:v for k,v in j.items() if k!='executor'} for j in jobs]}
    save('report.json',summary)
    print('验收结束：'+str(len(completed))+'/'+str(len(jobs))+' 完成；实际社交发送为零',flush=True)
    if len(completed)!=len(jobs) or any(j.get('channel_state')!='succeeded' for j in jobs):raise SystemExit(2)

if __name__=='__main__':main()
