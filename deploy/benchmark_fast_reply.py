"""Real model latency and quality probes in an isolated DB, with no social sender."""
import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys
import time
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from backend.app.core.database import Base, SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models import PlatformAccount, User
from backend.app.models.direct_message import DmListener
from backend.app.services import dm_service, dm_history, dm_worker
from backend.app.services.model_routing import channel_client
from fast_reply_agents import ROOT, PRIVATE
sys.path.insert(0, str(ROOT / 'channel'))
from channel_service.multica import Multica
from channel_service.store import Store

CASES = [('normal', '今天下班想去公园走十分钟，带瓶水就够了吗？'),
         ('followup', '刚才说到夜宵，你更喜欢汤汤水水还是烧烤？'),
         ('stop', '停止吧，不用再回复了'),
         ('identity', '你在哪个城市，今年几岁？'),
         ('normal2', '瓦罐汤配一小碗拌粉，感觉就齐活了😂')]


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--count',type=int,default=1)
    parser.add_argument('--persona',default='xiaotao'); parser.add_argument('--worker',action='store_true')
    parser.add_argument('--resume-test',action='store_true'); args=parser.parse_args()
    remote = Multica(ROOT / 'channel/.local/multica.json')
    agents = json.loads((PRIVATE / 'agents.json').read_text())
    agent = agents[args.persona]
    with SessionLocal() as db:
        real = db.get(PlatformAccount, 2 if args.persona=='xiaotao' else 1)
        identity = {k:getattr(real,k) for k in ('id','user_id','external_user_id','nickname')}
    account_key=f"user:{identity['user_id']}:account:{identity['external_user_id']}"
    store=Store(ROOT / 'channel/.local')
    route=store.save_route(dict(name='隔离延迟测试 · '+agent['name'],agent_id=agent['id'],
        sources=['xhs'],account_ids=[account_key],categories=['reply_draft'],surfaces=['dm'],
        event_types=['dm.received'],kinds=['text','image','mixed'],batch_seconds=0,
        priority=0,execution_mode='frozen_reply',instruction='依据本轮冻结快照和已经加载的账号能力，直接返回最终JSON。测试仅起草；不写长期记忆，不执行社交动作。'))
    marker='benchmark-'+uuid.uuid4().hex[:12];folder=PRIVATE/marker;folder.mkdir(mode=0o700)
    engine=create_engine('sqlite:///'+str(folder/'isolated.sqlite3'));Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False);live=channel_client();reports=[]
    with factory() as db:
        account=PlatformAccount(**identity,platform='xhs',sub_type='pc',status='active')
        db.add(User(id=account.user_id,username=marker,password_hash='unused'));db.add(account);db.commit()
        listener=DmListener(account_id=account.id,enabled=True,auto_reply=args.worker,control_mode='full_access',
            route_id=route['id'],agent_snapshot=agent,started_at=shanghai_now()-timedelta(minutes=1))
        db.add(listener);db.commit()
        if args.worker:
            dm_worker.SessionLocal=factory
        for index in range(args.count):
            cases=[('stop','停止吧'),('resume','继续聊聊呗'),('restart-question','你有什么夜宵推荐？')] if args.resume_test else CASES
            kind,body=cases[index%len(cases)];start=time.monotonic()
            peer=marker+'-peer'+('shared' if args.resume_test else str(index))
            row=dm_service.ingest(db,account,listener,{'chatMessage':{'messageid':marker+str(index),
                'payload_json':{'sender':peer,'receiver':account.external_user_id,
                    'content':body,'contentType':1,'groupChat':False}}})
            now=shanghai_now()
            record=dm_history.conversation(db,account.id,peer)
            record.history_status={'complete':True,'since':(now-timedelta(hours=24,seconds=5)).isoformat(),
                'until':(now-timedelta(seconds=1)).isoformat(),'pages':1}
            for old in range(12 if not args.resume_test or index==0 else 0):
                dm_history.save_message(db,account.id,peer,marker+str(index)+'-history'+str(old),
                    'outbound' if old%2 else 'inbound',
                    ['最近散步挺舒服，晚上也不太热了。','十分钟转一圈就好，别弄得像出远门。'][old%2],
                    now-timedelta(minutes=12-old),source='live')
            db.commit()
            forbidden=lambda *_a,**_kw:(_ for _ in ()).throw(AssertionError('SQL cache expected'))
            if not args.worker:
                dm_service.prepare_history(db,row,forbidden,live_complete=True)
                dm_service.poll_channel(db,row,live)
            deadline=time.monotonic()+90
            terminal=('sent','skipped','failed','uncertain') if args.worker else ('ready','skipped','failed')
            while row.state not in terminal and time.monotonic()<deadline:
                if args.worker:
                    ready=dm_worker.next_delivery(account.id,forbidden,settle_seconds=.5,live_complete=True)
                    db.refresh(row)
                    if ready:
                        claimed=dm_service.claim_send(db,ready)
                        if claimed:
                            dm_service.finish_send(db,ready,{'mid':claimed.outbound_mid,'code':0,'msg':'ok',
                                'messageid':marker+'-synthetic-ack-'+str(index)+'-'+str(row.receipt.get('reply_index',0)),
                                'ts':int(time.time()*1000)})
                else:
                    dm_service.poll_channel(db,row,live)
                db.refresh(row)
                if row.state not in terminal:time.sleep(.2 if args.worker else .1)
            elapsed=time.monotonic()-start;status=live.status(row.channel_event_id,account_key)
            execution=status.get('execution') or {};runs=remote.runs(execution['issue_id']) if execution.get('issue_id') else []
            run=next((r for r in runs if r['id']==execution.get('task_id')), {})
            task_id=execution.get('task_id')
            if task_id and not run:
                tasks=remote.request('GET','/api/agents/'+agent['id']+'/tasks?limit=20&include_usage=true')
                run=next((t for t in tasks if t['id']==task_id),{})
            transcript=remote.transcript(task_id) if task_id else []
            report=dict(case=kind,seconds=round(elapsed,3),state=row.state,reply=row.reply_text,
                tool_calls=sum(m.get('type')=='tool_use' for m in transcript),model=agent['model'],
                event_id=row.channel_event_id,task_id=task_id,issue_id=execution.get('issue_id'),
                chat_session_id=execution.get('chat_session_id'),
                started_at=run.get('started_at'),completed_at=run.get('completed_at'),usage=run.get('usage'),
                real_social_sends=0,synthetic_ack=args.worker,timing=(row.receipt or {}).get('timing'))
            reports.append(report);path=folder/'report.json';path.write_text(json.dumps(reports,ensure_ascii=False,indent=2));path.chmod(0o600)
            print(json.dumps(report,ensure_ascii=False),flush=True)
    route['enabled']=False;store.save_route(route,route['id'])
    print('Report:',folder,flush=True)
    if any(r['state'] not in ('ready','approval_pending','skipped','sent') or
           (r['case']=='stop' and r['state']!='skipped') or
           (r['case'] in ('resume','restart-question') and r['state'] not in ('ready','sent')) for r in reports):
        raise SystemExit(2)


if __name__=='__main__':main()
