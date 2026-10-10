"""Conversation-local evidence to prevent generated style feeding back into itself."""
from datetime import datetime, timedelta
from sqlalchemy import select, or_, and_
from backend.app.models import PlatformAccount
from backend.app.models.direct_message import DirectMessage, DmHistory
from backend.app.core.time import SHANGHAI_TZ


def context(db, row, snapshot):
    own = db.get(PlatformAccount, row.account_id)
    counterpart = db.scalar(select(PlatformAccount).where(PlatformAccount.user_id == own.user_id,
        PlatformAccount.platform == 'xhs', PlatformAccount.sub_type == 'pc',
        PlatformAccount.external_user_id == row.peer_id).order_by(PlatformAccount.id)) if own else None
    scopes = [and_(DirectMessage.account_id == row.account_id, DirectMessage.peer_id == row.peer_id)]
    if counterpart:
        scopes.append(and_(DirectMessage.account_id == counterpart.id, DirectMessage.peer_id == own.external_user_id))
    since = datetime.fromisoformat(snapshot['history']['since'])
    if since.tzinfo:
        since = since.astimezone(SHANGHAI_TZ).replace(tzinfo=None)
    jobs = db.scalars(select(DirectMessage).where(or_(*scopes), DirectMessage.created_at >= since - timedelta(minutes=1))).all()
    confirmed = []
    for job in jobs:
        receipt = job.receipt or {}
        plan = receipt.get('reply_messages') or []
        if not plan and job.state == 'sent' and receipt.get('code') == 0 and receipt.get('msg') == 'ok':
            plan = [{'status': 'sent', 'text': job.reply_text, 'ack': receipt}]
        for bubble in plan:
            ack = bubble.get('ack') or {}
            if bubble.get('status') == 'sent' and ack.get('code') == 0 and ack.get('msg') == 'ok':
                confirmed.append((job.account_id, bubble.get('text', ''), ack))
    automated, style = [], []
    for message in snapshot.get('messages', []):
        local = None
        mid = message['message_id']
        if mid.startswith('xhs-history:'):
            suffix = mid.split(':', 1)[1]
            if suffix.isdigit():
                local = db.get(DmHistory, int(suffix))
                if local and (local.account_id != row.account_id or local.peer_id != row.peer_id):
                    local = None
        stamp = datetime.fromisoformat(message['sent_at'])
        if stamp.tzinfo:
            stamp = stamp.astimezone(SHANGHAI_TZ).replace(tzinfo=None)
        direction_account = row.account_id if message['direction'] == 'outbound' else counterpart.id if counterpart else None
        generated = False
        for account_id, text, ack in confirmed:
            if account_id != direction_account:
                continue
            ack_mid = ack.get('messageid')
            if ack_mid and (local.message_id if local else mid) == ack_mid:
                generated = True
                break
            ts = ack.get('ts')
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                ack_time = datetime.fromtimestamp(ts / 1000 if ts > 10**11 else ts, SHANGHAI_TZ).replace(tzinfo=None)
                if message.get('text') == text and abs((stamp - ack_time).total_seconds()) <= 2:
                    generated = True
                    break
        if generated:
            automated.append(mid)
        elif message['direction'] == 'outbound' and message.get('text') and not message.get('revoked'):
            style.append({'message_id': mid, 'text': message['text'][:200], 'provenance': 'unconfirmed'})
    return {'known_automated_message_ids': automated,
            'self_style_candidates': style[-5:],
            'peer_is_managed_account': bool(counterpart),
            'style_policy': '已确认的自动生成消息仅作对话事实，不作为本人风格训练样本；未标记不代表已确认人工。'}
