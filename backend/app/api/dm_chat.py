import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.time import shanghai_now
from backend.app.models import User
from backend.app.models.direct_message import DmManualMessage, DmMedia
from backend.app.api.direct_messages import owned_account, Resolution
from backend.app.services import dm_chat as chat, dm_history as history, dm_media

router=APIRouter(prefix='/xhs/chat',tags=['xhs-chat'])

class Connect(BaseModel):
    enabled: bool

class Refresh(BaseModel):
    peer_id: str | None = Field(default=None,min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_-]+$')

class Send(BaseModel):
    client_id: str = Field(min_length=1,max_length=80,pattern=r'^[A-Za-z0-9_-]+$')
    expected_external_user_id: str = Field(min_length=1,max_length=128)
    peer_id: str = Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_-]+$')
    text: str = Field(default='',max_length=2000)
    asset_id: str | None = Field(default=None,min_length=1,max_length=64)

class Control(BaseModel):
    peer_id: str = Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_-]+$')
    enabled: bool

@router.get('/conversations')
def conversations(account_id:int,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    account=owned_account(db,user,account_id)
    row=db.get(chat.DmListener,account_id)
    return {'items':chat.conversations(db,account_id),'connection':{
        'state':row.state if row else 'stopped','error':row.error if row else '',
        'manual_enabled':bool(row and row.manual_chat_enabled),'auto_reply':bool(row and row.enabled and row.auto_reply)}}

@router.post('/accounts/{account_id}/connect')
def connect(account_id:int,data:Connect,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    account=owned_account(db,user,account_id);row=chat.listener(db,account)
    row.manual_chat_enabled=data.enabled
    if data.enabled and row.state=='stopped':row.state='connecting'
    db.commit();return {'enabled':row.manual_chat_enabled}

@router.post('/accounts/{account_id}/refresh')
def refresh(account_id:int,data:Refresh,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    account=owned_account(db,user,account_id);row=chat.listener(db,account);row.manual_chat_enabled=True
    if data.peer_id:
        record=history.conversation(db,account_id,data.peer_id)
        record.history_status={**record.history_status,'sync_requested':True}
    else:row.chat_refresh_requested=True
    db.commit();return {'state':'queued'}

@router.get('/messages')
def messages(account_id:int,peer_id:str=Query(min_length=1,max_length=128),user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    owned_account(db,user,account_id)
    record=db.scalar(select(history.DmConversation).where(history.DmConversation.account_id==account_id,
        history.DmConversation.peer_id==peer_id))
    return {'items':chat.timeline(db,account_id,peer_id),'syncing':bool(record and record.history_status.get('sync_requested')),
        'manual_control':bool(record and record.session_state.get('manual_control'))}

@router.post('/accounts/{account_id}/control')
def control(account_id:int,data:Control,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    owned_account(db,user,account_id);chat.set_control(db,account_id,data.peer_id,data.enabled)
    return {'manual_control':data.enabled}

@router.post('/accounts/{account_id}/send')
def send(account_id:int,data:Send,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    account=owned_account(db,user,account_id)
    if account.external_user_id!=data.expected_external_user_id:
        raise HTTPException(409,'账号身份已变化，请刷新账号后重新发送')
    return chat.view(chat.queue(db,account,data.client_id,data.peer_id,data.text,data.asset_id))

@router.get('/requests/{client_id}')
def request_status(client_id:str,account_id:int,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    owned_account(db,user,account_id)
    row=db.scalar(select(DmManualMessage).where(DmManualMessage.account_id==account_id,DmManualMessage.client_id==client_id))
    if not row:raise HTTPException(404,'尚未找到该发送请求，可使用原请求重试')
    return chat.view(row)

@router.post('/accounts/{account_id}/images')
async def image(account_id:int,file:UploadFile=File(...),user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    owned_account(db,user,account_id)
    blob=await file.read(dm_media.LIMIT+1)
    try:dm_media.validate_image(blob)
    except dm_media.MediaUnavailable as exc:raise HTTPException(400,str(exc)) from None
    asset=DmMedia(id=uuid.uuid4().hex,account_id=account_id,kind='image',label='图片',content_type=2,
        native={},url='',library=False)
    db.add(asset);db.flush();dm_media.store_blob(db,asset,blob)
    return dm_media.view(asset)

@router.post('/requests/{id}/resolve')
def resolve(id:int,data:Resolution,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    row=db.get(DmManualMessage,id)
    if not row:raise HTTPException(404,'消息不存在')
    owned_account(db,user,row.account_id)
    if row.state not in ('queued','uncertain'):raise HTTPException(409,'消息状态已变化，请刷新')
    if data.outcome=='confirmed_sent' and row.state!='uncertain':raise HTTPException(409,'仅待核对的消息可确认收到')
    target='sent' if data.outcome=='confirmed_sent' else 'cancelled'
    result=db.execute(update(DmManualMessage).where(DmManualMessage.id==row.id,DmManualMessage.state==row.state)
        .values(state=target,receipt={**row.receipt,'manually_resolved':data.outcome},error=''))
    if result.rowcount!=1:db.rollback();raise HTTPException(409,'消息状态已变化，请刷新')
    db.refresh(row)
    if row.state=='sent':
        saved=history.save_message(db,row.account_id,row.peer_id,row.receipt.get('messageid') or row.outbound_mid,
            'outbound',row.text,history.timestamp(row.receipt.get('ts')) or row.created_at,source='receipt',
            content_type=2 if row.asset_id else 1,media=[dm_media.view(db.get(DmMedia,row.asset_id))] if row.asset_id else [])
        row.receipt={**row.receipt,'history_row_id':saved.id}
    db.commit();return chat.view(row)
