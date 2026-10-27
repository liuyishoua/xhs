import json
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from backend.app.core.database import SessionLocal
from backend.app.core.deps import get_current_user
from backend.app.models import LoginSession, User
from backend.app.services import sms_relay as relay

router = APIRouter(prefix='/sms-relay', tags=['SMS relay'])


@router.get('/setup', response_class=HTMLResponse)
def setup():
    return HTMLResponse((Path(__file__).parents[1] / 'static/sms-relay.html').read_text(), headers={'Cache-Control': 'no-store'})


@router.get('/settings')
def settings(user: User = Depends(get_current_user)):
    return JSONResponse({**relay.settings(user.id),
        'url': 'https://d.58nft.net/mobile-sms/inbound/' + str(user.id)},
        headers={'Cache-Control': 'no-store'})


class SenderSettings(BaseModel):
    xhs_senders: list[str] = Field(max_length=8)


@router.put('/settings')
def update_settings(data: SenderSettings, user: User = Depends(get_current_user)):
    senders = list(dict.fromkeys(s.strip() for s in data.xhs_senders))
    if any(not s or len(s) > 64 for s in senders):
        raise HTTPException(400, 'Invalid sender')
    relay.set_senders(user.id, senders)
    return {'success': True}


def deliver(user_id, content, sender=''):
    pending, state = relay.claim(user_id, content, sender)
    if not pending:
        return {'success': True, 'state': state}
    try:
        from backend.app.api.login_sessions import _confirm_phone_login, PhoneConfirmRequest
        from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
        from backend.app.adapters.xhs.creator_login_adapter import XhsCreatorLoginAdapter
        with SessionLocal() as db:
            session = db.get(LoginSession, pending['session_id'])
            user = db.get(User, user_id)
            if not session or not user or session.user_id != user_id or session.platform != 'xhs' or session.status != 'pending':
                raise ValueError('login no longer pending')
            sub_type = session.sub_type
            if sub_type not in ('pc', 'creator'):
                raise ValueError('unsupported login')
            adapter = XhsPcLoginAdapter() if sub_type == 'pc' else XhsCreatorLoginAdapter()
            _confirm_phone_login(PhoneConfirmRequest(session_id=session.id, phone=pending['phone'], code=pending['code']),
                user, db, adapter, sub_type, XhsCreatorLoginAdapter() if sub_type == 'pc' else None)
        state = 'confirmed'
    except Exception:
        # The session may have been accepted remotely. Never retry a code on
        # transport errors, and never return platform errors containing secrets.
        state = 'needs_attention'
    relay.complete(pending, state)
    return {'success': True, 'state': state}


@router.post('/inbound/{user_id}')
async def inbound(user_id: int, request: Request):
    if request.headers.get('origin'):
        raise HTTPException(403, 'Machine requests only')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            raise HTTPException(413, 'Message too large')
    try:
        if request.headers.get('content-type', '').startswith('application/json'):
            data = json.loads(raw)
        else:
            data = {k: v[0] for k, v in parse_qs(raw.decode(), keep_blank_values=True).items()}
        stamp, sign = str(data.get('timestamp', '')), str(data.get('sign', ''))
        content = data.get('content', '')
        sender = data.get('from', '')
        if not isinstance(content, str) or len(content) > 4096 or not isinstance(sender, str) or len(sender) > 64:
            raise ValueError()
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
        raise HTTPException(400, 'Invalid message') from None
    if not await run_in_threadpool(relay.authenticate, user_id, stamp, sign):
        raise HTTPException(401, 'Invalid signature or expired timestamp')
    return await run_in_threadpool(deliver, user_id, content, sender)
