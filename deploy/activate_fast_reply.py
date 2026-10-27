"""Switch only owned XHS DM routes, retaining listener and conversation controls."""
import json
from pathlib import Path
import sys
import time
from sqlalchemy import select
from backend.app.core.database import SessionLocal
from backend.app.models import PlatformAccount, ModelConfig
from backend.app.models.direct_message import DmListener
from fast_reply_agents import ROOT, PRIVATE
sys.path.insert(0, str(ROOT / 'channel'))
from channel_service.store import Store


def main():
    agents=json.loads((PRIVATE/'agents.json').read_text());store=Store(ROOT/'channel/.local')
    before=[];changes=[]
    with SessionLocal() as db:
        for aid,key in [(1,'xhs-leo'),(2,'xiaotao')]:
            account=db.get(PlatformAccount,aid);listener=db.get(DmListener,aid)
            if not account or account.platform!='xhs' or account.sub_type!='pc' or not listener:
                raise ValueError('Expected owned PC account/listener missing')
            before.append({'account_id':aid,'route_id':listener.route_id,'agent_snapshot':listener.agent_snapshot})
            agent=agents[key];account_key=f'user:{account.user_id}:account:{account.external_user_id}'
            route=store.bind_agent('xhs',account_key,'dm',agent)
            route=store.save_route({**route,'execution_mode':'frozen_reply','batch_seconds':0,'max_batch':1,
                'instruction':'只用本轮完整冻结快照直接起草最终私信JSON，身份与对话事实隔离。'},route['id'])
            snapshot={**agent,'route_id':route['id'],'route_version':route['version']}
            listener.route_id,listener.agent_snapshot=route['id'],snapshot
            for config in db.scalars(select(ModelConfig).where(ModelConfig.model_type=='dm',
                ModelConfig.provider=='multica-channel',ModelConfig.user_id==account.user_id,
                ModelConfig.base_url=='account:'+str(aid))):
                config.model_name,config.name=route['id'],agent['name']
            changes.append({'account_id':aid,'name':account.nickname,'route_id':route['id'],
                'agent':agent['name'],'model':agent['model']})
        path=PRIVATE/('before-activation-'+str(time.time_ns())+'.json')
        path.write_text(json.dumps(before,ensure_ascii=False,indent=2));path.chmod(0o600)
        db.commit()
    (PRIVATE/'activation.json').write_text(json.dumps(changes,ensure_ascii=False,indent=2))
    print(json.dumps(changes,ensure_ascii=False))


if __name__=='__main__':main()
