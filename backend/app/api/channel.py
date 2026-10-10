"""Authenticated XHS workspace bridge to the independent Channel service."""
from pathlib import Path
from typing import Any
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models import PlatformAccount, User
from backend.app.services.channel_client import ChannelClient, ChannelError

router=APIRouter(prefix='/channel',tags=['channel'])
CONFIG=Path(__file__).resolve().parents[3]/'.local/web/channel.json'


class MessageRequest(BaseModel):
    message_id:str=Field(min_length=8,max_length=160)
    account_id:int|None=None
    text:str=Field(default='',max_length=16000)
    images:list[str]=Field(default_factory=list,max_length=6)
    context:dict[str,Any]=Field(default_factory=dict)
    conversation_id:str=Field(default='',max_length=160)
    category:str=Field(default='content_analysis',max_length=80)


def identity(user,db,account_id):
    if account_id is None:return 'user:'+str(user.id)
    account=db.get(PlatformAccount,account_id)
    if not account or account.user_id!=user.id:raise HTTPException(404,'平台账号不存在')
    return 'user:'+str(user.id)+':account:'+str(account.external_user_id or account.id)


def client():
    try:return ChannelClient(CONFIG)
    except ChannelError as exc:raise HTTPException(503,str(exc)) from None


@router.post('/messages',status_code=202)
def submit(data:MessageRequest,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    account=identity(user,db,data.account_id)
    try:return client().send(account,data.message_id,data.text,data.images,data.category,data.conversation_id,data.context,surface='content',event_type='content.rewrite_requested')
    except ChannelError as exc:raise HTTPException(502,str(exc)) from None


@router.get('/messages/{event_id}')
def status(event_id:str,account_id:int|None=None,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    try:return client().status(event_id,identity(user,db,account_id))
    except ChannelError as exc:raise HTTPException(502,str(exc)) from None


@router.post('/assets')
async def upload(file:UploadFile=File(...),account_id:int|None=Form(None),user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    blob=await file.read(8*1024*1024+1)
    try:return await run_in_threadpool(client().upload,identity(user,db,account_id),blob,file.filename or 'image.png')
    except ChannelError as exc:raise HTTPException(502,str(exc)) from None
