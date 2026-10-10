from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from datetime import timedelta
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.time import shanghai_now
from backend.app.models import PlatformAccount, User
from backend.app.models.direct_message import DmListener, DirectMessage
from backend.app.services.dm_service import listener_view, message_view, reset_analysis
from backend.app.services import dm_history, dm_media
from backend.app.models.direct_message import DmMedia
from backend.app.services import xhs_network
from backend.app.models.direct_message import DmConversation
from backend.app.services.model_routing import default_config, PROVIDER, validate_route

router = APIRouter(prefix='/xhs/direct-messages', tags=['xhs-direct-messages'])


class ListenerRequest(BaseModel):
    enabled: bool
    auto_reply: bool = False
    route_id: str = Field(default='', max_length=80)
    control_mode: Literal['request_approval', 'auto_approve', 'full_access'] | None = None


class Resolution(BaseModel):
    outcome: Literal['confirmed_sent', 'skip']


def owned_account(db, user, account_id):
    account = db.get(PlatformAccount, account_id)
    if not account or account.user_id != user.id or account.platform != 'xhs' or account.sub_type != 'pc':
        raise HTTPException(404, '小红书 PC 账号不存在')
    return account


@router.get('/network')
def network_status(user: User = Depends(get_current_user)):
    return xhs_network.status()


@router.post('/network/check')
def check_network(user: User = Depends(get_current_user)):
    return xhs_network.check()


@router.get('/listeners')
def listeners(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(DmListener).join(PlatformAccount, PlatformAccount.id == DmListener.account_id)
        .where(PlatformAccount.user_id == user.id)).all()
    holds = {}
    if rows:
        blocked = db.scalars(select(DirectMessage).where(
            DirectMessage.account_id.in_([row.account_id for row in rows]),
            DirectMessage.state.in_(['uncertain', 'held'])).order_by(DirectMessage.id)).all()
        for message in blocked:
            conversations = holds.setdefault(message.account_id, {})
            conversations.setdefault(message.peer_id, dict(message_id=message.id, peer_id=message.peer_id,
                state=message.state, reason=message.error))
    return {'items': [{**listener_view(row), 'conversation_holds': list(holds.get(row.account_id, {}).values())} for row in rows]}


@router.put('/listeners/{account_id}')
def configure(account_id: int, data: ListenerRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    account = owned_account(db, user, account_id)
    row = db.get(DmListener, account_id)
    if data.enabled:
        if not account.external_user_id:
            raise HTTPException(400, '请先重新绑定 PC 账号以取得用户 ID')
        config = default_config(db, user.id, 'dm')
        route_id = data.route_id or (config.model_name if config and config.provider == PROVIDER else '')
        if not route_id:
            raise HTTPException(400, '请先在模型配置选择私信回复路由')
        validate_route(route_id, 'dm')
    else:
        route_id = row.route_id if row else ''
    if row is None:
        row = DmListener(account_id=account_id)
        db.add(row)
    if data.enabled and not row.enabled:
        row.started_at = shanghai_now()
    row.enabled = data.enabled
    row.auto_reply = data.auto_reply
    if data.control_mode is not None:
        row.control_mode = data.control_mode
    row.route_id = route_id
    row.state = 'connecting' if data.enabled else 'stopping'
    row.error = ''
    db.commit()
    db.refresh(row)
    return listener_view(row)


@router.get('')
def messages(account_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    owned_account(db, user, account_id)
    rows = db.scalars(select(DirectMessage).where(DirectMessage.account_id == account_id)
        .order_by(DirectMessage.id.desc()).limit(100)).all()
    return {'items': [message_view(row) for row in rows]}


@router.post('/{message_id}/resolve')
def resolve(message_id: int, data: Resolution, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.get(DirectMessage, message_id)
    if not row:
        raise HTTPException(404, '消息不存在')
    owned_account(db, user, row.account_id)
    if row.state not in ('uncertain', 'held', 'ready', 'approval_pending'):
        raise HTTPException(409, '只有待核对、待批准、特殊事项或待发送的消息可以处理')
    if data.outcome == 'confirmed_sent' and row.state != 'uncertain':
        raise HTTPException(409, '仅发送结果待核对的消息可以确认已发送')
    result = db.execute(update(DirectMessage).where(DirectMessage.id == row.id,
        DirectMessage.state == row.state).values(
        state='sent' if data.outcome == 'confirmed_sent' else 'skipped',
        receipt={**row.receipt, 'manually_resolved': data.outcome, 'resolved_by': user.id}, error=''))
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(409, '消息状态已变化，请刷新后处理')
    db.commit()
    db.refresh(row)
    return message_view(row)


@router.get('/history')
def history(account_id: int, peer_id: str = Query(min_length=1, max_length=128),
            user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    owned_account(db, user, account_id)
    now = shanghai_now()
    rows = dm_history.history_rows(db, account_id, peer_id, now - timedelta(hours=48), now)
    record = db.scalar(select(DmConversation).where(DmConversation.account_id == account_id,
        DmConversation.peer_id == peer_id))
    return dict(items=[dm_history.view(m) for m in rows],
        history=record.history_status if record else {}, analysis=record.analysis if record else {})


@router.post('/{message_id}/reanalyze')
def reanalyze(message_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.get(DirectMessage, message_id)
    if not row:
        raise HTTPException(404, '消息不存在')
    owned_account(db, user, row.account_id)
    if row.state not in ('held', 'failed', 'ready', 'history_error', 'approval_pending'):
        raise HTTPException(409, '当前消息不可重新分析')
    from backend.app.services.dm_service import sent_plan_ids
    if sent_plan_ids(row):
        raise HTTPException(409, '本轮已有已发消息，请取消余下回复或等待对方新消息')
    # Atomic reservation protects a ready row against the dispatcher's send claim.
    result = db.execute(update(DirectMessage).where(DirectMessage.id == row.id,
        DirectMessage.state == row.state).values(state='history_pending').execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(409, '消息状态已变化，请刷新后处理')
    # reset_analysis commits the new immutable attempt in this same transaction.
    reset_analysis(db, row)
    db.refresh(row)
    return message_view(row)


@router.post('/{message_id}/approve')
def approve(message_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from sqlalchemy.orm import aliased
    row = db.get(DirectMessage, message_id)
    if not row:
        raise HTTPException(404, '消息不存在')
    owned_account(db, user, row.account_id)
    if row.state != 'approval_pending':
        raise HTTPException(409, '该回复当前不需要批准')
    newer = aliased(DirectMessage)
    result = db.execute(update(DirectMessage).where(DirectMessage.id == row.id,
        DirectMessage.state == 'approval_pending', ~select(newer.id).where(
            newer.account_id == DirectMessage.account_id, newer.peer_id == DirectMessage.peer_id,
            newer.id > DirectMessage.id).exists()).values(state='ready', receipt={**row.receipt,
        'approval': {'message_id': row.channel_payload['message_id'], 'approved_by': user.id,
                     'approved_at': shanghai_now().isoformat()}}))
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(409, '已收到更新消息或状态已变化，请刷新')
    db.commit()
    db.refresh(row)
    return message_view(row)


@router.get('/media/{asset_id}')
def media_file(asset_id: str, account_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    owned_account(db, user, account_id)
    asset = db.get(DmMedia, asset_id)
    if not asset or asset.account_id != account_id:
        raise HTTPException(404, '图片不存在')
    try:
        path = dm_media.local_file(db, asset)
    except dm_media.MediaUnavailable as exc:
        raise HTTPException(422, str(exc)) from None
    return FileResponse(path, media_type=asset.mime, headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})
