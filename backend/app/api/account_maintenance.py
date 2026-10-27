"""Scoped maintenance control; cookies stay in the workbench."""
import json
from pathlib import Path
import sys

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.app.core.database import get_db
from backend.app.models import PlatformAccount, User, LoginSession, ApiLog
from backend.app.api import accounts, login_sessions

ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT/'account-maintenance'))
from bridge import Bridge

router=APIRouter(prefix='/maintenance',tags=['account maintenance control'])
DIRECTORY=Path(__file__).resolve().parents[3]/'.local'


def control(request:Request):
    bridge=Bridge(DIRECTORY)
    if not bridge.authorized(request.headers):raise HTTPException(401,'Maintenance credential required')
    return bridge


def owner(db,bridge):
    user=db.get(User,int(bridge.config['user_id']))
    if not user:raise HTTPException(409,'Maintenance owner unavailable')
    return user


def selected(db,user,id,external):
    if not str(id).isdigit():raise HTTPException(400,'Invalid account')
    a=db.get(PlatformAccount,int(id))
    if not a or a.user_id!=user.id or a.platform!='xhs' or a.external_user_id!=external:raise HTTPException(409,'Account identity mismatch')
    return a


@router.get('/accounts')
def directory(bridge=Depends(control),db=Depends(get_db)):
    user=owner(db,bridge)
    return {'items':[{'id':str(a.id),'external_id':a.external_user_id,'name':a.nickname,'kind':a.sub_type,
      'status':a.status,'capability':'sms','login_methods':['sms','qr','cookie'],'url':'https://d.58nft.net:9450/platforms/xhs/accounts'}
      for a in db.scalars(select(PlatformAccount).where(PlatformAccount.user_id==user.id,PlatformAccount.platform=='xhs')) if a.external_user_id]}


@router.get('/events')
def events(after:int=0,bridge=Depends(control),db=Depends(get_db)):
    user=owner(db,bridge)
    rows=db.scalars(select(ApiLog).where(ApiLog.user_id==user.id,ApiLog.platform=='xhs',
      ApiLog.endpoint=='internal/xhs/account-session',ApiLog.id>max(after,int(bridge.config.get('event_after',0))),
      ApiLog.status.in_(['active','expired','risk','disconnected'])).order_by(ApiLog.id).limit(100)).all()
    items=[]
    for row in rows:
        meta=row.meta or {};a=db.get(PlatformAccount,meta.get('account_id'))
        # This existing log commits atomically with the account status. Never
        # generate login work from the old periodic account health check.
        state={'active':'connected','expired':'expired','risk':'risk','disconnected':'disconnected'}[row.status]
        eligible=row.status=='active' and meta.get('source')!='scheduled_check' or row.status in ('expired','risk') and meta.get('source') in ('dm_websocket','creator_identity_check','creator_upload','comment_identity_check','publisher_preflight')
        if not a or not meta.get('external_id') or not eligible:
            state='connected'
        items.append({'cursor':row.id,'event_id':'xhs-log:'+str(row.id),'account_id':str(meta.get('account_id','')),
          'external_id':str(meta.get('external_id') or (a.external_user_id if a else '')),'state':state,
          'component':meta.get('component','account'),
          'reason_code':{'connected':'connection_restored','expired':'auth_expired','risk':'verification_required','disconnected':'listener_disconnected'}[state],
          'ignored':not a or not meta.get('external_id') or not eligible,
          **({'business_code':meta['business_code']} if type(meta.get('business_code')) is int else {})})
    return {'items':items}


@router.get('/attempts/{id}')
def attempt(id:str,bridge=Depends(control)):
    r=bridge.attempt(id)
    if not r:raise HTTPException(404,'Attempt absent')
    return {'state':r['state'],'remote_id':r['remote_id'],'updated':r['updated'],**json.loads(r['result'])}


class Start(BaseModel):
    request_id:str=Field(min_length=1,max_length=128)
    account_id:str=Field(max_length=32)
    external_id:str=Field(max_length=128)
    phone:str=Field(pattern=r'^\d{11}$')
    force_login:bool=False


class ListenerRepair(BaseModel):
    account_id:str=Field(max_length=32)
    external_id:str=Field(max_length=128)
    component:str=Field(default='listener.dm',max_length=64)


@router.post('/repair-listener')
def repair_listener(data:ListenerRepair,bridge=Depends(control),db=Depends(get_db)):
    user=owner(db,bridge);a=selected(db,user,data.account_id,data.external_id)
    if data.component=='listener.dm':
        from backend.app.models.direct_message import DmListener
        listener=db.get(DmListener,a.id)
        if not listener or not (listener.enabled or listener.manual_chat_enabled):return {'state':'inactive'}
    checked=accounts.check_account(a.id,user,db,accounts.get_pc_account_adapter(),accounts.get_creator_account_adapter(),accounts.get_xhs_self_profile_adapter())
    return {'state':{'active':'connected','expired':'expired','risk':'risk'}.get(checked['status'],'unknown')}


@router.post('/start')
def start(data:Start,bridge=Depends(control),db=Depends(get_db)):
    user=owner(db,bridge);a=selected(db,user,data.account_id,data.external_id)
    try:r,fresh=bridge.reserve(data.request_id,a.id,a.external_user_id,data.phone,{'force_login':True} if data.force_login else None)
    except ValueError:raise HTTPException(409,'Request identity conflict') from None
    if not fresh:return {'state':r['state'],'remote_id':r['remote_id'],**json.loads(r['result'])}
    # Preserve verified Cookie refreshes before asking the owner for SMS.
    checked={'status':a.status} if data.force_login else accounts.check_account(a.id,user,db,accounts.get_pc_account_adapter(),accounts.get_creator_account_adapter(),accounts.get_xhs_self_profile_adapter())
    if checked['status']=='active' and not data.force_login:
        bridge.update(data.request_id,'confirmed',result={'reason':'现有登录态已核验可用'})
        return {'state':'confirmed'}
    if checked['status'] not in ('expired','risk') and not data.force_login:
        bridge.update(data.request_id,'needs_attention',result={'reason':'网络或平台状态未确认，未发送验证码'})
        return {'state':'needs_attention','reason':'网络或平台状态未确认，未发送验证码'}
    payload=login_sessions.PhoneSendCodeRequest(phone=data.phone,account_id=a.id,sync_creator=False)
    from backend.app.services.sms_relay import maintenance_managed
    managed_token=maintenance_managed.set(True)
    try:
        result=(login_sessions.pc_phone_send_code(payload,user,db,login_sessions.get_pc_login_adapter()) if a.sub_type=='pc'
          else login_sessions.creator_phone_send_code(payload,user,db,login_sessions.get_creator_login_adapter()))
        bridge.update(data.request_id,'waiting',str(result['session_id']))
        return {'state':'waiting','remote_id':str(result['session_id'])}
    except Exception:
        bridge.update(data.request_id,'uncertain',result={'reason':'平台发码响应未确认，请核对手机短信和工作台，未自动重试'})
        return {'state':'uncertain','reason':'平台发码响应未确认，请核对手机短信和工作台，未自动重试'}
    finally:maintenance_managed.reset(managed_token)


class Confirm(Start):
    code:str=Field(pattern=r'^\d{4,8}$')


@router.post('/confirm')
def confirm(data:Confirm,bridge=Depends(control),db=Depends(get_db)):
    user=owner(db,bridge);a=selected(db,user,data.account_id,data.external_id)
    r=bridge.attempt(data.request_id)
    if not r or r['account_id']!=str(a.id) or r['external_id']!=a.external_user_id:raise HTTPException(409,'Attempt identity mismatch')
    try:bridge.reserve(data.request_id,a.id,a.external_user_id,data.phone,{'force_login':True} if data.force_login else None)
    except ValueError:raise HTTPException(409,'Request identity conflict') from None
    if not bridge.claim_confirmation(data.request_id):return {'state':r['state'],'remote_id':r['remote_id'],**json.loads(r['result'])}
    try:
        s=db.get(LoginSession,int(r['remote_id']))
        if not s or s.status!='pending' or s.user_id!=user.id:raise ValueError('Session not pending')
        payload=login_sessions.PhoneConfirmRequest(session_id=s.id,phone=data.phone,code=data.code,sync_creator=False)
        login_sessions._confirm_phone_login(payload,user,db,login_sessions.get_pc_login_adapter() if a.sub_type=='pc' else login_sessions.get_creator_login_adapter(),a.sub_type,None)
        checked=accounts.check_account(a.id,user,db,accounts.get_pc_account_adapter(),accounts.get_creator_account_adapter(),accounts.get_xhs_self_profile_adapter())
        state='confirmed' if checked['status']=='active' else 'needs_attention'
        reason='账号身份及登录权限已核验' if state=='confirmed' else '登录已返回，平台仍要求额外验证，请查看工作台账号状态'
        bridge.update(data.request_id,state,r['remote_id'],{'reason':reason})
        from backend.app.services.sms_relay import connect
        with connect() as relay_db:
            relay_db.execute('UPDATE pending SET state=? WHERE session_id=?',(state,int(r['remote_id'])))
        return {'state':state,'reason':reason}
    except HTTPException as exc:
        if 'HTTP 471' in str(exc.detail):
            reason='小红书要求额外安全验证（HTTP 471）；自动短信续登已暂停，请在官方网页完成验证后通过工作台扫码或 Cookie 导入恢复'
            bridge.update(data.request_id,'needs_user',r['remote_id'],{'reason':reason})
            from backend.app.services.sms_relay import connect
            with connect() as relay_db:
                relay_db.execute('UPDATE pending SET state=? WHERE session_id=?',('needs_user',int(r['remote_id'])))
            return {'state':'needs_user','reason':reason}
        bridge.update(data.request_id,'uncertain',r['remote_id'],{'reason':'确认登录未完成，未自动重交验证码'})
        return {'state':'uncertain','reason':'确认登录未完成，未自动重交验证码'}
    except Exception:
        bridge.update(data.request_id,'uncertain',r['remote_id'],{'reason':'确认登录未完成，未自动重交验证码'})
        return {'state':'uncertain','reason':'确认登录未完成，未自动重交验证码'}
