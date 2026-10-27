"""Discovery outbox on the existing IM worker, with ordered, replay-safe pairs."""
import json
import uuid

from sqlalchemy import select

from backend.app.models import PlatformAccount
from backend.app.models.direct_message import DmManualMessage
from backend.app.services import dm_chat


def queue_pair(db, account, item, body):
    if dm_chat.controlled(db, account.id, item['author_id']):
        raise ValueError('此会话已由本人接管，未发送主动私信')
    messages = ([('note_link', body['share_note_url'])] if body.get('share_note', True) else []) + [('text', body['text'])]
    rows = []
    for index, (kind, text) in enumerate(messages):
        client_id = f"proactive_{item['id']}_{index}"
        row = db.scalar(select(DmManualMessage).where(
            DmManualMessage.account_id == account.id, DmManualMessage.client_id == client_id))
        if row is None:
            row = DmManualMessage(account_id=account.id, client_id=client_id,
                external_user_id=account.external_user_id, peer_id=item['author_id'],
                text=text, outbound_mid=str(uuid.uuid4()), state='queued',
                receipt={'proactive_item_id': item['id'], 'proactive_index': index, 'proactive_kind': kind})
            db.add(row)
            db.flush()
        elif (row.external_user_id, row.peer_id, row.text) != (account.external_user_id, item['author_id'], text):
            raise ValueError('主动私信请求与已保存内容不一致，未重复排队')
        rows.append(row)
    # Connect IM without changing incoming auto-reply or claiming human authorship.
    connection = dm_chat.listener(db, account)
    connection.manual_chat_enabled = True
    db.commit()
    return [row.id for row in rows]


def permit(db, message, account):
    """Recheck the durable strategy immediately before the existing worker claims it."""
    from backend.app.services import proactive_interactions as service
    item_id = message.receipt.get('proactive_item_id')
    if not item_id:
        return True
    with service.store() as ledger:
        item = ledger.execute('''SELECT i.*,r.config,r.owner_id,r.state AS run_state
            FROM items i JOIN runs r ON r.id=i.run_id WHERE i.id=?''', (item_id,)).fetchone()
    reason = ''
    if (not item or item['interaction'] != 'dm' or item['account_id'] != account.id
            or item['identity'] != account.external_user_id or item['author_id'] != message.peer_id
            or item['owner_id'] != account.user_id
            or item['state'] not in ('sending', 'pending_delivery')
            or item['run_state'] not in ('running', 'waiting_delivery')):
        reason = '主动私信任务或账号已变化，未发送'
    else:
        original = json.loads(item['config'])
        current = service.settings(account.id, original['source'], account.user_id)
        if current['revision'] != original.get('revision') or (original.get('scheduled') and not current['enabled']):
            reason = '主动私信设置已变化，未发送'
        elif message.peer_id in service.stopped_peers(account) or dm_chat.controlled(db, account.id, message.peer_id):
            reason = '此用户已停止联系或会话已接管，未发送'
        elif message.peer_id in set(db.scalars(select(PlatformAccount.external_user_id).where(PlatformAccount.platform == 'xhs', PlatformAccount.user_id == account.user_id))):
            reason = '目标是已连接账号，未发送'
    if reason:
        message.state, message.error = 'cancelled', reason
        db.commit()
        return False
    if account.status != 'active':
        return False  # Leave the durable queue intact until verified login recovers.
    if not service.within_window(original):
        return False  # Keep unsent messages for the next allowed daily window.
    index = message.receipt.get('proactive_index', 0)
    if index > 0:
        previous = db.scalar(select(DmManualMessage).where(
            DmManualMessage.account_id == account.id,
            DmManualMessage.client_id == f'proactive_{item_id}_{index - 1}'))
        if not previous or previous.state != 'sent':
            if not previous or previous.state in ('failed', 'cancelled', 'uncertain'):
                message.state, message.error = 'cancelled', '笔记分享未确认成功，未继续发送私信'
                db.commit()
            return False
    return True


def reconcile():
    """Expose only actual IM acknowledgments, never equate queueing with sending."""
    from backend.app.services import proactive_interactions as service
    with service.store() as ledger:
        items = [dict(row) for row in ledger.execute(
            "SELECT * FROM items WHERE interaction='dm' AND state='pending_delivery'")]
    for item in items:
        body = json.loads(item['body'])
        with service.SessionLocal() as db:
            rows = [db.scalar(select(DmManualMessage).where(
                DmManualMessage.account_id == item['account_id'],
                DmManualMessage.client_id == f"proactive_{item['id']}_{index}"))
                for index in range(2 if body.get('share_note', True) else 1)]
            if any(row is None for row in rows):
                state, detail = 'uncertain', '私信发送记录不完整，请核对；不自动重发'
            else:
                first = rows[0]
                # A failed/uncertain share must not leave a runnable second message.
                if len(rows) > 1 and first.state in ('failed', 'uncertain', 'cancelled') and rows[1].state == 'queued':
                    rows[1].state, rows[1].error = 'cancelled', '笔记分享未确认成功，未继续发送私信'
                    db.commit()
                if any(row.state in ('queued', 'sending') for row in rows):
                    continue
                body['messages'] = [{'kind': row.receipt.get('proactive_kind', 'text'),
                    'state': row.state, 'external_id': str(row.receipt.get('messageid') or ''),
                    'detail': row.error, 'sent_at': row.receipt.get('ts')}
                    for index, row in enumerate(rows)]
                if all(row.state == 'sent' for row in rows):
                    state, detail = 'submitted', ('笔记链接及私信均收到平台成功回执' if body.get('share_note', True) else '文字私信收到平台成功回执') + '；未核验对方已读'
                    body['sent_at'] = max(row.updated_at for row in rows).isoformat()
                elif any(row.state == 'uncertain' for row in rows):
                    state, detail = 'uncertain', '私信发送结果待核对，不自动重发'
                elif first.state == 'sent':
                    state, detail = 'partial', '笔记链接已提交，文字私信未发送成功；不自动重发'
                elif all(row.state == 'cancelled' for row in rows):
                    state, detail = 'cancelled', '设置或会话状态变化，未发送'
                else:
                    state, detail = 'failed', ('平台未接受笔记分享，未继续发送私信' if body.get('share_note', True) else '平台未接受文字私信')
            external = ','.join(str(row.receipt.get('messageid')) for row in rows if row and row.state == 'sent' and row.receipt.get('messageid'))
        with service.store() as ledger:
            ledger.execute("UPDATE items SET state=?,detail=?,body=?,external_id=? WHERE id=? AND state='pending_delivery'",
                           (state, detail, service.dump(body), external, item['id']))
    with service.store() as ledger:
        for run in ledger.execute("SELECT id FROM runs WHERE state='waiting_delivery'").fetchall():
            states = [row[0] for row in ledger.execute('SELECT state FROM items WHERE run_id=?', (run['id'],))]
            if not states or any(state in ('ready', 'sending', 'pending_delivery') for state in states):
                continue
            state = ('uncertain' if 'uncertain' in states else 'partial' if 'partial' in states
                     or ('submitted' in states and any(s != 'submitted' for s in states))
                     else 'failed' if 'failed' in states else 'cancelled' if 'cancelled' in states else 'completed')
            count = states.count('submitted')
            ledger.execute('UPDATE runs SET state=?,detail=?,updated_at=? WHERE id=?',
                (state, f'已完成 {count} 位作者的私信；详细状态请查看互动结果', service.stamp(), run['id']))
