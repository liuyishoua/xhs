"""Select a newly created live Agent through the workbench API in an isolated DB."""
from pathlib import Path
from datetime import datetime,timedelta
import json,time,uuid
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from backend.app.core.database import Base,SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models import User,PlatformAccount
from backend.app.models.direct_message import DmListener,DirectMessage
from backend.app.api.direct_messages import configure,ListenerRequest,agents
from backend.app.services import dm_service,dm_sessions
from backend.app.services.model_routing import channel_client
from backend.app.services.content_automation import PRIVATE,ROOT,Multica
from deploy.smoke_live_account_agents import BOUNDARY


def main():
    test_agent=json.loads((ROOT/'channel/.local/agent-smoke/dynamic-directory/report.json').read_text())
    live=channel_client();remote=Multica(ROOT/'channel/.local/multica.json')
    marker='synthetic-dynamic-smoke-'+uuid.uuid4().hex[:10]
    folder=PRIVATE/('live-dynamic-agent-smoke-'+datetime.now().strftime('%Y%m%d-%H%M%S'));folder.mkdir(mode=0o700)
    def save(name,data):
        path=folder/name;path.write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str)+'\n');path.chmod(0o600)
    with SessionLocal() as production:
        real=production.scalar(select(PlatformAccount).where(PlatformAccount.platform=='xhs',PlatformAccount.sub_type=='pc',PlatformAccount.nickname=='leo'))
        identity=dict(id=real.id,user_id=real.user_id,external_user_id=real.external_user_id,nickname=real.nickname)
    engine=create_engine('sqlite:///'+str(folder/'isolated-workbench.sqlite3'));Base.metadata.create_all(engine);factory=sessionmaker(bind=engine,expire_on_commit=False)
    with factory() as db:
        user=User(id=identity['user_id'],username='synthetic-owner',password_hash='unused');account=PlatformAccount(**identity,platform='xhs',sub_type='pc',status='active')
        db.add_all([user,account]);db.commit()
        directory=agents(account.id,user,db)
        assert any(a['id']==test_agent['agent_id'] for a in directory['agents'])
        result=configure(account.id,ListenerRequest(enabled=True,auto_reply=False,control_mode='auto_approve',agent_id=test_agent['agent_id']),user,db)
        assert result['agent_id']==test_agent['agent_id']
        save('isolated-listener-binding.json',{'directory':directory,'listener':result,'account':account.nickname})
        listener=db.get(DmListener,account.id);listener.started_at=shanghai_now()-timedelta(minutes=1);db.commit()
        row=dm_service.ingest(db,account,listener,{'chatMessage':{'messageid':marker,'payload_json':{'sender':marker+'-peer','receiver':account.external_user_id,'content':'虚构目录验收聊天：今天准备去公园走十分钟，带水就够了吗？','contentType':1,'groupChat':False}}})
        dm_service.prepare_history(db,row,lambda *_a,**_kw:{'result':{'success':True},'out_message_list':[]})
        assert dm_sessions.prepare(db,row,live)
        row.channel_payload={**row.channel_payload,'text':row.channel_payload['text']+BOUNDARY,'context':{**row.channel_payload['context'],'synthetic_smoke':True,'test_marker':marker,'long_term_memory_writes':False}}
        db.commit();dm_service.poll_channel(db,row,live)
        print('新建并改名的动态 Agent 已经由工作台显式绑定，私信已提交 live Channel',flush=True)
        deadline=time.monotonic()+600
        while time.monotonic()<deadline and row.state=='waiting':
            time.sleep(5);dm_service.poll_channel(db,row,live)
        status=live.status(row.channel_event_id,row.channel_payload['account_id']);execution=status.get('execution') or {}
        save('live-channel-history.json',status);save('isolated-workbench-history.json',dm_service.message_view(row))
        runs=remote.runs(execution['issue_id']) if execution.get('issue_id') else []
        matching=[r for r in runs if r['id']==execution.get('task_id')]
        transcript=remote.transcript(execution['task_id']) if execution.get('task_id') else []
        save('multica-runs.json',runs);save('multica-transcript.json',transcript)
        report={'marker':marker,'account':account.nickname,'account_id':account.id,'dynamic_agent_name':test_agent['name'],
            'agent_id':test_agent['agent_id'],'route_id':result['route_id'],'issue_id':execution.get('issue_id'),'task_id':execution.get('task_id'),
            'channel_state':status['state'],'workbench_state':row.state,'live_channel':True,'real_multica':True,'isolated_workbench':True,'real_social_sends':0,
            'actual_run_agent_id':matching[0].get('agent_id') if matching else None,'run_status':matching[0].get('status') if matching else None,
            'skill_file_read_mentions':json.dumps(transcript,ensure_ascii=False).count('SKILL.md')}
        save('report.json',report)
        for path in folder.glob('*'):
            if path.is_file():path.chmod(0o600)
        print('动态 Agent Channel='+status['state']+' 隔离工作台='+row.state,flush=True)
        if status['state']!='succeeded' or report['actual_run_agent_id']!=test_agent['agent_id']:raise SystemExit(2)

if __name__=='__main__':main()
