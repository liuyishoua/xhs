"""Independent discovery strategies using the existing comment and IM transports."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool, field_validator
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models import User
from backend.app.services import proactive_interactions as service

router = APIRouter(prefix='/xhs/proactive', tags=['proactive interactions'])


class SettingsRequest(BaseModel):
    instruction: str = Field(default='', max_length=1000)
    enabled: StrictBool = False
    interval_minutes: int = Field(default=60, ge=1, le=10080)
    browse_limit: int = Field(default=3, ge=1, le=20)
    comment_limit: int = Field(default=1, ge=1, le=10)
    keyword: str = Field(default='', max_length=100)
    dm_limit: int = Field(default=1, ge=1, le=10)
    comments_per_note: int = Field(default=1, ge=1, le=10)
    share_note: StrictBool = True
    filter_ads: StrictBool = True
    daily_start: str = Field(default='00:00', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    daily_end: str = Field(default='23:59', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    agent_id: str = Field(default='', max_length=80)

    @field_validator('keyword', 'agent_id')
    @classmethod
    def clean(cls, value):
        return value.strip()

@router.get('/{account_id}')
def status(account_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    service.owned(db, user.id, account_id)
    return service.snapshot(user.id, account_id)


@router.get('/{account_id}/agents')
def agents(account_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    account = service.owned(db, user.id, account_id)
    return service.agent_catalog(account)


@router.put('/{account_id}/settings/{source}')
def configure(account_id: int, source: Literal['recommend', 'visitors', 'outreach'], body: SettingsRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    account = service.owned(db, user.id, account_id)
    if source == 'outreach' and not body.keyword:
        raise HTTPException(422, '请填写主动私信探索的搜索主题')
    previous = service.settings(account_id, source, user.id)
    if body.agent_id and not body.enabled and body.agent_id == previous.get('agent_id'):
        snapshot = previous.get('agent_snapshot', {})  # Pausing remains possible if Channel is down.
    else:
        snapshot = service.agent_config(account, body.agent_id)._agent_snapshot if body.agent_id else {}
    return service.save(user.id, account_id, body.model_dump() | {
        'identity': account.external_user_id, 'source': source, 'agent_snapshot': snapshot})


@router.post('/{account_id}/run/{source}', status_code=202)
def run_once(account_id: int, source: Literal['recommend', 'visitors', 'outreach'], user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        account = service.owned(db, user.id, account_id)
        if account.status not in ('active', 'expired', 'risk'):
            raise ValueError('请先恢复此账号登录')
        config = service.settings(account_id, source, user.id)
        if not config['revision']:
            raise ValueError('请先保存本轮设置')
        if config.get('identity') != account.external_user_id:
            raise ValueError('账号身份已变化，请重新保存设置')
        if not service.within_window(config):
            raise ValueError('当前不在此策略的每日运行时段内（北京时间）')
        return {'id': service.enqueue(user.id, account, config)}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


