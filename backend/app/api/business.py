"""Dedicated headquarters principal is recognized only by these endpoints."""
from __future__ import annotations
import asyncio
import hmac
import json
from pathlib import Path
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from backend.app.core.database import SessionLocal
from backend.app.core.security import decode_token, token_matches_user
from backend.app.models import PlatformAccount, User, AiDraft, PublishJob, PublishAsset
from backend.app.models.direct_message import DmConversation, DmHistory, DirectMessage, DmListener
from backend.app.api.platforms.xhs.pc import _get_owned_pc_account_cookies, _normalize_search_item
from backend.app.adapters.xhs.pc_api_adapter import XhsPcApiAdapter
from backend.app.services.business_tasks import BusinessLedger, SUPPORTED, UNAVAILABLE

ROOT=Path(__file__).resolve().parents[3]
CONFIG=ROOT/'.local/business-api.json'
router=APIRouter(prefix='/business',tags=['business'])
_ledger=None

def ledger():
    global _ledger
    if _ledger is None: _ledger=BusinessLedger(ROOT/'.local/business-tasks.sqlite3')
    return _ledger

def machine_config():
    try: return json.loads(CONFIG.read_text())
    except (OSError,ValueError): return {}

def principal(request:Request):
    auth=request.headers.get('authorization','')
    token=auth[7:] if auth.startswith('Bearer ') else ''
    config=machine_config()
    if token and config.get('token') and hmac.compare_digest(token,str(config['token'])):
        return 'machine:headquarters'
    if token:
        from backend.app.core.ops_identity import endpoint, validate, local_user
        if endpoint():
            identity = validate(token, request.cookies.get('ops_workspace_session', ''))
            with SessionLocal() as db:
                return 'user:' + str(local_user(db, identity).id)
        try:
            data=decode_token(token)
            if data.get('token_type')=='access':
                with SessionLocal() as db:
                    if token_matches_user(data, db.get(User,data['user_id'])): return 'user:'+str(data['user_id'])
        except Exception: pass
    raise HTTPException(401,'Business API authentication required')

def policy(p):
    if p=='machine:headquarters':
        cfg=machine_config()
        return int(cfg['owner_id']), set(map(str,cfg.get('account_ids',[]))), set(cfg.get('operations',[]))
    return int(p.split(':')[1]), None, SUPPORTED

def check(p,body):
    owner,accounts,operations=policy(p)
    if body['operation'] not in operations or (accounts is not None and str(body['account_id']) not in accounts): raise ValueError('principal operation/account denied')
    with SessionLocal() as db:
        a=db.get(PlatformAccount,int(body['account_id']))
        if not a or a.user_id!=owner or a.platform!='xhs' or a.sub_type!='pc': raise ValueError('owned PC account required')
        if body['operation'] in {'profile.read','content.read','public.search','dm.reply','content.publish','comment.reply'} and a.status!='active': raise ValueError('platform account inactive')
        if body['operation']=='dm.reply':
            listener=db.get(DmListener,a.id)
            if not listener or not listener.enabled or not listener.auto_reply or listener.control_mode=='request_approval': raise ValueError('automatic reply not enabled under current control')
            from backend.app.services import xhs_network
            network=xhs_network.status()
            if listener.state!='listening' or (network['mode']=='home' and network['state']!='online'): raise ValueError('DM sending connection is not online')

def execute(p,body):
    owner,_,_=policy(p); account_id=int(body['account_id']); operation=body['operation']; args=body['input']
    with SessionLocal() as db:
        user=db.get(User,owner); account=db.get(PlatformAccount,account_id)
        if operation in {'profile.read','content.read','public.search'}:
            adapter=XhsPcApiAdapter(_get_owned_pc_account_cookies(db,user,account_id))
            if operation=='profile.read':
                data=adapter.get_self_info()
                if str(data.get('user_id'))!=str(account.external_user_id) or data.get('guest'): raise ValueError('platform identity mismatch')
                result={k:data.get(k) for k in ('user_id','nickname','desc','avatar','gender','ip_location')}
                coverage='current_authenticated_profile'
            elif operation=='content.read':
                raw=adapter.get_recent_user_notes(account.external_user_id)
                result={'items':[_public_note(n) for n in raw.get('notes',[])], 'has_more':bool(raw.get('has_more'))}
                coverage='recent_first_page'
            else:
                keyword=str(args.get('keyword','')).strip()
                if not keyword or len(keyword)>200: raise ValueError('keyword required')
                page=int(args.get('page',1))
                if not 1<=page<=20: raise ValueError('page outside range')
                success,_,raw=adapter.search_note(keyword,page=page)
                if not success: raise ValueError('platform search not confirmed')
                data=(raw or {}).get('data') or {}
                result={'keyword':keyword,'page':page,'items':[_public_note(_normalize_search_item(x)) for x in data.get('items',[]) if isinstance(x,dict) and x.get('model_type') not in ('rec_query','hot_query')], 'has_more':bool(data.get('has_more'))}
                coverage='requested_public_search_page'
            source='authenticated_platform_sdk'
        elif operation=='conversation.read':
            rows=db.scalars(select(DmConversation).where(DmConversation.account_id==account_id).order_by(DmConversation.updated_at.desc()).limit(100)).all()
            result={'items':[{'conversation_id':r.peer_id,'updated_at':r.updated_at.isoformat()} for r in rows]}; source='workbench_cache';coverage='latest_100_cached_conversations'
        elif operation=='message.read':
            peer=str(args['conversation_id'])
            rows=db.scalars(select(DmHistory).where(DmHistory.account_id==account_id,DmHistory.peer_id==peer).order_by(DmHistory.id.desc()).limit(100)).all()
            result={'conversation_id':peer,'items':[{'message_id':r.message_id,'direction':r.direction,'text':r.text,'sent_at':r.sent_at.isoformat(),'source':r.source} for r in reversed(rows)]}
            source='workbench_cache';coverage='latest_100_cached_messages'
        elif operation=='material.read':
            job=db.get(PublishJob,int(args['publish_job_id']));creator=db.get(PlatformAccount,job.platform_account_id) if job else None
            if not job or job.user_id!=owner or not creator or creator.user_id!=owner or not account.external_user_id or creator.external_user_id!=account.external_user_id: raise ValueError('owned same-account material job required')
            assets=db.scalars(select(PublishAsset).where(PublishAsset.publish_job_id==job.id)).all()
            result={'publish_job_id':job.id,'title':job.title,'body':job.body,'status':job.status,'assets':[{'asset_id':x.id,'asset_type':x.asset_type,'upload_status':x.upload_status} for x in assets]}
            source='workbench_cache';coverage='exact_owned_material_job'
        elif operation=='content.publish':
            from backend.app.services.business_operations import publish
            return publish(db,ledger(),p,body,account,user)
        elif operation=='comment.reply':
            from backend.app.services.business_operations import comment
            adapter=XhsPcApiAdapter(_get_owned_pc_account_cookies(db,user,account_id))
            return comment(db,ledger(),p,body,account,user,adapter)
        elif operation=='content.analyze':
            from backend.app.services.model_routing import channel_client, resolve_agent
            client=channel_client();route,snapshot=resolve_agent(client.catalog(),'content',account,str(args.get('agent_id','')))
            text=str(args.get('text','')).strip()
            if not text or len(text)>16000: raise ValueError('analysis instruction required within limits')
            response=client.send(account_id='user:'+str(owner)+':account:'+str(account.external_user_id or account.id),message_id='business-'+body['request_id'],text=text,category='content_analysis',surface='content',event_type='content.rewrite_requested',route_id=route['id'],context={'agent_snapshot':snapshot})
            return {'_pending':'waiting_model','source':'channel_model_request','coverage':'model_intermediate_only','account_id':account_id,'data':{'channel_event_id':response['id']}}
        elif operation=='dm.reply':
            row=db.scalar(select(DirectMessage).where(DirectMessage.account_id==account_id,DirectMessage.message_id==str(args.get('message_id','')),DirectMessage.peer_id==str(args.get('conversation_id',''))))
            if not row or row.state not in ('history_pending','history_error','queued'): raise ValueError('existing incoming reply task not eligible')
            if row.channel_event_id or (row.receipt or {}).get('channel_request') or row.channel_payload.get('session_snapshot_id'): raise ValueError('model request already submitted; cannot rebind')
            old=(row.receipt or {}).get('business_task')
            with ledger().connect() as connection:
                pending=connection.execute('SELECT id FROM tasks WHERE principal=? AND request_id=?',(p,body['request_id'])).fetchone()
            link={'task_id':pending['id'],'principal':p,'goal_id':body['goal_id'],'project_id':body['project_id'],'action_id':body['action_id'],'account_id':account_id,'operation':operation,'authorization_revision':body['authorization_revision']}
            if old and old['task_id']!=link['task_id']: raise ValueError('incoming already bound to another task')
            row.receipt={**(row.receipt or {}),'business_task':link}
            row.channel_payload={**row.channel_payload,'message_id':'business-'+body['request_id']}
            db.commit()
            return {'_pending':'waiting_model','source':'workbench_dm_job','coverage':'existing_incoming_message','data':{'direct_message_id':row.id,'channel_event_id':row.channel_event_id,'message_id':row.message_id,'conversation_id':row.peer_id}}
        elif operation=='draft.create':
            title=str(args.get('title','')).strip(); text=str(args.get('body','')).strip()
            if not title or len(title)>256 or not text or len(text)>16000: raise ValueError('draft title/body required within limits')
            # This is a workbench artifact only, never a publication receipt.
            row=AiDraft(user_id=owner,platform='xhs',title=title,body=text)
            db.add(row); db.commit(); db.refresh(row)
            ledger().register_created(p,body,'draft',row.id)
            publish_job_id=None
            asset_ids=args.get('uploaded_asset_ids',[])
            if asset_ids:
                creator=db.scalar(select(PlatformAccount).where(PlatformAccount.user_id==owner,PlatformAccount.platform=='xhs',PlatformAccount.sub_type=='creator',PlatformAccount.external_user_id==account.external_user_id,PlatformAccount.status=='active'))
                if not creator: raise ValueError('active same identity creator required for supplied assets')
                selected=[]
                with ledger().connect() as connection:
                    authority=json.loads(connection.execute('SELECT body FROM authority WHERE principal=? AND goal=?',(p,body['goal_id'])).fetchone()['body'])
                grant=next(g for g in authority['grants'] if str(g['account_id'])==str(account_id) and 'draft.create' in g['operations'])
                for aid in asset_ids:
                    asset=db.get(PublishAsset,int(aid));original=db.get(PublishJob,asset.publish_job_id) if asset else None
                    if not original or original.user_id!=owner or original.platform_account_id!=creator.id or str(original.id) not in [str(x) for x in grant.get('scope',{}).get('resource_ids',[])] or asset.asset_type!='image' or asset.upload_status!='uploaded': raise ValueError('uploaded source assets outside exact owner/resource authority')
                    selected.append(asset)
                job=PublishJob(user_id=owner,platform='xhs',platform_account_id=creator.id,source_draft_id=row.id,title=title,body=text,publish_mode='immediate',status='pending')
                db.add(job);db.flush()
                for asset in selected: db.add(PublishAsset(publish_job_id=job.id,asset_type='image',file_path=asset.file_path,upload_status='uploaded',creator_media_id=asset.creator_media_id,creator_upload_info=asset.creator_upload_info))
                db.commit();publish_job_id=job.id;ledger().register_created(p,body,'publish_job',job.id)
            result={'draft_id':row.id,'publish_job_id':publish_job_id,'account_id':account_id,'title':title,'body':text,'published':False}
            source='workbench_draft';coverage='local_draft_only'
        else: raise ValueError('unsupported operation')
    import time
    return {'data':result,'source':source,'coverage':coverage,'account_id':account_id,'observed_at':time.time()}

def _public_note(n):
    return {k:n[k] for k in ('id','note_id','title','display_title','content','desc','type','note_url','user','interact_info') if k in n}

class Submit(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_id:str=Field(min_length=1,max_length=150)
    goal_id:str=Field(min_length=1,max_length=200)
    project_id:str=Field(min_length=1,max_length=200)
    action_id:str=Field(min_length=1,max_length=200)
    account_id:int=Field(gt=0)
    operation:str
    input:dict[str,Any]=Field(default_factory=dict)
    authorization_revision:int=Field(ge=0,strict=True)
    origin:str='headquarters'

@router.get('/capabilities')
def capabilities(p:str=Depends(principal)):
    owner,allowed,operations=policy(p)
    with SessionLocal() as db:
        accounts=db.scalars(select(PlatformAccount).where(PlatformAccount.user_id==owner,PlatformAccount.platform=='xhs',PlatformAccount.sub_type=='pc')).all()
        resources=[]
        for a in accounts:
            if allowed is not None and str(a.id) not in allowed: continue
            conversation_ids=[r.peer_id for r in db.scalars(select(DmConversation).where(DmConversation.account_id==a.id)).all()]
            ops=[]
            for op in sorted(SUPPORTED|set(UNAVAILABLE)):
                reason=UNAVAILABLE.get(op,'')
                if op not in operations and not reason: reason='principal not authorized'
                if op=='dm.reply':
                    listener=db.get(DmListener,a.id)
                    if not listener or not listener.enabled or not listener.auto_reply or listener.control_mode=='request_approval': reason='现有私信自动回复未开启或控制方式需要人工批准'
                    elif listener.state!='listening': reason='私信WebSocket尚未在线监听：'+listener.state
                    else:
                        from backend.app.services import xhs_network
                        network=xhs_network.status()
                        if network['mode']=='home' and network['state']!='online': reason='私信发送所需网络尚未在线'
                if op in {'profile.read','content.read','public.search','dm.reply','content.publish','comment.reply'} and a.status!='active': reason='platform account inactive'
                schema={'type':'object','properties':{}}
                if op=='public.search': schema.update(properties={'keyword':{'type':'string','maxLength':200},'page':{'type':'integer','minimum':1,'maximum':20}},required=['keyword'])
                if op in {'message.read','dm.reply'}: schema.update(properties={'conversation_id':{'type':'string'}},required=['conversation_id'])
                if op=='dm.reply': schema['properties']['message_id']={'type':'string'}; schema['required'].append('message_id'); schema['properties']['instruction']={'type':'string','maxLength':2000}
                if op=='material.read': schema.update(properties={'publish_job_id':{'type':'integer'}},required=['publish_job_id'])
                if op=='content.publish': schema.update(properties={'publish_job_id':{'type':'integer'},'draft_id':{'type':'integer'}},anyOf=[{'required':['publish_job_id']},{'required':['draft_id']}])
                if op=='comment.reply': schema.update(properties={'note_id':{'type':'string'},'target_comment_id':{'type':'string'},'text':{'type':'string','maxLength':2000}},required=['note_id','target_comment_id','text'])
                if op=='content.analyze': schema.update(properties={'agent_id':{'type':'string'},'text':{'type':'string','maxLength':16000}},required=['agent_id','text'])
                if op=='draft.create': schema.update(properties={'title':{'type':'string','maxLength':256},'body':{'type':'string','maxLength':16000},'uploaded_asset_ids':{'type':'array','items':{'type':'integer'}}},required=['title','body'])
                if op=='content.publish':
                    creator=db.scalar(select(PlatformAccount).where(PlatformAccount.user_id==owner,PlatformAccount.platform=='xhs',PlatformAccount.sub_type=='creator',PlatformAccount.external_user_id==a.external_user_id,PlatformAccount.status=='active'))
                    if not creator or not a.external_user_id: reason='该PC账号尚无同owner同身份可用Creator会话'
                ops.append({'id':op,'label':op,'available':not reason,'reason':reason,'input_schema':schema})
            from backend.app.api.channel import CONFIG as CHANNEL_CONFIG
            try: channel_source=json.loads(CHANNEL_CONFIG.read_text()).get('source_id','')
            except (OSError,ValueError): channel_source=''
            resources.append({'channel_source_id':channel_source,'channel_account_id':'user:'+str(owner)+':account:'+str(a.external_user_id or a.id),'account_id':a.id,'name':a.nickname or str(a.id),'operations':ops,'scope_options':{'conversation_ids':[{'id':s,'label':s} for s in conversation_ids],'resource_ids':[{'id':str(j.id),'label':j.title} for j in db.scalars(select(PublishJob).join(PlatformAccount,PlatformAccount.id==PublishJob.platform_account_id).where(PublishJob.user_id==owner,PlatformAccount.user_id==owner,PlatformAccount.external_user_id==a.external_user_id)).all()]}})
    return {'resources':resources}

@router.put('/goals/{goal_id}/authority')
def authority(goal_id:str,body:dict,p:str=Depends(principal)):
    try:
        if not body.get('project_id'): raise ValueError('project_id required to bind goal')
        if body.get('enabled'):
            owner,accounts,operations=policy(p)
            for g in body.get('grants',[]):
                with SessionLocal() as db:
                    account=db.get(PlatformAccount,int(g['account_id']))
                    if not account or account.user_id!=owner or account.platform!='xhs' or account.sub_type!='pc' or (accounts is not None and str(account.id) not in accounts): raise ValueError('principal account denied')
                if any(op not in operations for op in g['operations']): raise ValueError('principal operation denied')
        return ledger().set_authority(p,goal_id,body)
    except (ValueError,KeyError,TypeError): raise HTTPException(409,'Invalid authority or stale revision') from None

@router.post('/tasks',status_code=202)
def submit(body:Submit,p:str=Depends(principal)):
    try:
        value=body.model_dump()
        if value['origin']!='headquarters': raise ValueError('origin invalid')
        check(p,value)
        return ledger().submit(p,value)
    except (ValueError,KeyError,TypeError): raise HTTPException(409,'Business task denied, unavailable or request conflict') from None

@router.get('/tasks/{task_id}')
def task(task_id:str,p:str=Depends(principal)):
    try: return ledger().get(p,task_id)
    except KeyError: raise HTTPException(404,'Business task not found') from None

@router.post('/tasks/{task_id}/cancel')
def cancel(task_id:str,p:str=Depends(principal)):
    try: return ledger().cancel(p,task_id)
    except KeyError: raise HTTPException(404,'Business task not found') from None

@router.get('/events')
def events(after:int=0,goal_id:str|None=None,p:str=Depends(principal)):
    if after<0: raise HTTPException(400,'invalid cursor')
    return ledger().events(p,after,goal_id)

async def run():
    ledger().recover()
    while True:
        try:
            await asyncio.to_thread(ledger().tick,execute,check)
            await asyncio.to_thread(reconcile_models)
            await asyncio.to_thread(reconcile_dm)
            await asyncio.to_thread(scan_inbound)
        except Exception:
            import logging
            logging.getLogger(__name__).warning('Business worker transient failure; durable tasks retained')
        await asyncio.sleep(1)

def scan_inbound():
    """Only exact granted conversations with accepted tasks yield goal events."""
    with ledger().connect() as db:
        tasks=db.execute("SELECT * FROM tasks WHERE state NOT IN ('cancelled','failed')").fetchall()
    for task in tasks:
        body=json.loads(task['body'])
        if body['operation'] not in {'message.read','dm.reply'}: continue
        peer=str(body['input'].get('conversation_id',''))
        try:
            with ledger().connect() as db: ledger().validate(db,task['principal'],body)
            check(task['principal'],body)
        except (ValueError,KeyError): continue
        with SessionLocal() as session:
            rows=session.scalars(select(DmHistory).where(DmHistory.account_id==int(body['account_id']),DmHistory.peer_id==peer,DmHistory.direction=='inbound',DmHistory.source!='history').order_by(DmHistory.id.desc()).limit(100)).all()
            for r in rows:
                # Only later observations, never imported historic facts.
                from zoneinfo import ZoneInfo
                if r.created_at.replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() < task['created']: continue
                ledger().inbound(task,peer,r.message_id,{'message_id':r.message_id,'conversation_id':peer,'text':r.text,'source':'workbench_inbound','coverage':'exact_linked_conversation','received_at':r.created_at.isoformat()})


def business_reply_guard(row):
    link=(row.receipt or {}).get('business_task')
    if not link: return True
    try:
        task=ledger().get(link['principal'],link['task_id'])
        if task['state'] not in ('running','waiting_model','waiting_platform'): return False
        if task['account_id']!=row.account_id or task['input'].get('conversation_id')!=row.peer_id or task['input'].get('message_id')!=row.message_id: return False
        with ledger().connect() as db: ledger().validate(db,link['principal'],task)
        check(link['principal'],task)
        return True
    except (ValueError,KeyError): return False

def reconcile_dm():
    with ledger().connect() as db:
        tasks=db.execute("SELECT * FROM tasks WHERE state IN ('waiting_model','waiting_platform')").fetchall()
    for task in tasks:
        body=json.loads(task['body'])
        if body['operation']!='dm.reply': continue
        with SessionLocal() as db:
            row=db.scalar(select(DirectMessage).where(DirectMessage.account_id==int(body['account_id']),DirectMessage.message_id==body['input']['message_id'],DirectMessage.peer_id==body['input']['conversation_id']))
            if not row: ledger().complete_pending(task['id'],'failed',{},'bound incoming message missing');continue
            if (row.receipt or {}).get('business_task',{}).get('task_id')!=task['id']: ledger().complete_pending(task['id'],'failed',{},'message binding changed');continue
            result={'source':'workbench_dm_job','coverage':'exact_bound_reply','account_id':row.account_id,'data':{'direct_message_id':row.id,'message_id':row.message_id,'conversation_id':row.peer_id,'channel_event_id':row.channel_event_id,'workbench_state':row.state}}
            if row.state=='sent':
                ack=row.receipt or {}
                if ack.get('mid')!=row.outbound_mid or type(ack.get('code')) is not int or ack['code']!=0 or ack.get('msg')!='ok':
                    ledger().complete_pending(task['id'],'uncertain',result,'matching platform ACK missing');continue
                result.update(source='authenticated_platform_ack',coverage='sent_reply_only')
                result['data'].update(external_id=ack.get('messageid') or row.outbound_mid,receipt={k:ack.get(k) for k in ('mid','messageid','code','msg','ts')})
                ledger().complete_pending(task['id'],'succeeded',result)
            elif row.state=='uncertain': ledger().complete_pending(task['id'],'uncertain',result,'platform write not confirmed')
            elif row.state in ('failed','superseded','skipped','held'):
                ledger().complete_pending(task['id'],'failed' if row.state!='held' else 'cancelled',result,'workbench reply stopped: '+row.state)
            elif not business_reply_guard(row):
                if row.state!='sending': row.state='held';row.error='business goal authority inactive';db.commit();ledger().complete_pending(task['id'],'cancelled',result,'business authorization revoked or expired')
            elif row.state in ('ready','approval_pending','sending'): ledger().complete_pending(task['id'],'waiting_platform',result)


def reconcile_models():
    from backend.app.services.model_routing import channel_client
    with ledger().connect() as db: tasks=db.execute("SELECT * FROM tasks WHERE state='waiting_model'").fetchall()
    for task in tasks:
        body=json.loads(task['body'])
        if body['operation']!='content.analyze': continue
        try:
            with ledger().connect() as db: ledger().validate(db,task['principal'],body)
            check(task['principal'],body)
        except (ValueError,KeyError): ledger().complete_pending(task['id'],'cancelled',{},'authorization revoked');continue
        result=json.loads(task['result']); event=result.get('data',{}).get('channel_event_id')
        if not event: ledger().complete_pending(task['id'],'uncertain',result,'model request outcome uncertain');continue
        owner,_,_=policy(task['principal'])
        with SessionLocal() as db:
            account=db.get(PlatformAccount,int(body['account_id']))
            identity='user:'+str(owner)+':account:'+str(account.external_user_id or account.id)
        try: status=channel_client().status(event,identity)
        except Exception: continue
        if status.get('state') in ('succeeded','completed'):
            model_result=status.get('result')
            output=model_result.get('output') or {} if isinstance(model_result,dict) else {}
            valid, reason=validate_model_product(output)
            if not valid:
                ledger().complete_pending(task['id'],'failed',{'source':'model_intermediate','coverage':'no_valid_business_artifact','data':{'channel_event_id':event,'validation':reason}},reason)
                continue
            ledger().complete_pending(task['id'],'succeeded',{'source':'model_intermediate','coverage':'analysis_draft_not_platform_action','data':{'channel_event_id':event,'output':output}})
        elif status['state'] in ('failed','cancelled'): ledger().complete_pending(task['id'],'failed',result,'model task did not complete')


def attach_reply_goal(db,row):
    link=(row.receipt or {}).get('business_task')
    if not link: return
    if not business_reply_guard(row): raise ValueError('business reply authority inactive')
    task=ledger().get(link['principal'],link['task_id'])
    instruction=str(task['input'].get('instruction',''))
    if len(instruction)>2000: raise ValueError('business instruction too long')
    context={**row.channel_payload.get('context',{}),'business_goal':{'goal_id':task['goal_id'],'project_id':task['project_id'],'action_id':task['action_id'],'instruction':instruction}}
    row.channel_payload={**row.channel_payload,'context':context}
    db.commit()


def validate_model_product(output):
    """Channel transport success is not proof that a useful draft was produced."""
    if not isinstance(output,dict): return False,'model output is not a business artifact'
    structured=output.get('structured')
    text=output.get('text')
    if not isinstance(structured,dict): structured={}
    if not structured and isinstance(text,str):
        candidate=text.strip()
        if candidate.startswith('```') and candidate.endswith('```'):
            candidate=candidate.split('\n',1)[-1].rsplit('```',1)[0].strip()
        try:
            decoded=json.loads(candidate)
            if isinstance(decoded,dict): structured=decoded
        except (ValueError,TypeError): pass
    failures={'needs_context','need_context','blocked','error','failed','requires_input','awaiting_input'}
    def failure(value):
        if isinstance(value,dict):
            status=value.get('status')
            if isinstance(status,str) and status.strip().lower() in failures: return status.strip().lower()
            if value.get('error') or value.get('blocked') is True or value.get('needs_context') is True: return 'error_or_missing_context'
            for child in value.values():
                reason=failure(child)
                if reason:return reason
        elif isinstance(value,list):
            for child in value:
                reason=failure(child)
                if reason:return reason
        return ''
    reason=failure(structured) or failure({k:v for k,v in output.items() if k!='text'})
    if reason:return False,'model did not produce a draft: '+reason
    product_keys={'text','body','content','analysis','summary','report','draft','recommendations','findings','answer'}
    def meaningful(value):
        if isinstance(value,str):return bool(value.strip())
        if isinstance(value,list):return any(meaningful(x) for x in value)
        if isinstance(value,dict):return any(meaningful(v) for k,v in value.items() if k in product_keys)
        return False
    if structured:
        return (True,'') if meaningful(structured) else (False,'model returned no valid business artifact')
    return (True,'') if isinstance(text,str) and text.strip() else (False,'model returned no valid business artifact')
