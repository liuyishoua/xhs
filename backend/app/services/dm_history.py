"""Account/peer isolated SQL history and immutable 24-hour model snapshots."""
from datetime import datetime, timedelta
import json
from sqlalchemy import select
from backend.app.core.time import SHANGHAI_TZ, shanghai_now
from backend.app.models.direct_message import DmHistory, DmConversation
from backend.app.services.channel_client import compact_context
from backend.app.services import dm_content, dm_media

HISTORY_WINDOW_HOURS = 24


class HistoryUnavailable(ValueError):
    pass


def timestamp(value):
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
        if seconds > 10**11:
            seconds /= 1000
        if seconds <= 0:
            return None
        return datetime.fromtimestamp(seconds, SHANGHAI_TZ).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def content_text(content):
    for _ in range(6):
        if isinstance(content, dict):
            content = content.get('content', content.get('text'))
        elif isinstance(content, str):
            try:
                parsed = json.loads(content)
            except ValueError:
                return content
            if isinstance(parsed, (dict, str)):
                content = parsed
            else:
                return content
        else:
            return ''
    return content if isinstance(content, str) else ''


def conversation(db, account_id, peer_id):
    row = db.scalar(select(DmConversation).where(DmConversation.account_id == account_id,
        DmConversation.peer_id == peer_id))
    if row is None:
        row = DmConversation(account_id=account_id, peer_id=peer_id, analysis={}, history_status={})
        db.add(row)
        db.flush()
    return row


def save_message(db, account_id, peer_id, message_id, direction, text, sent_at,
                 *, source='history', content_type=1, revoked=False, media=None):
    refs = [] if revoked else dm_media.register(db, account_id, media or [])
    row = db.scalar(select(DmHistory).where(DmHistory.account_id == account_id,
        DmHistory.peer_id == peer_id, DmHistory.message_id == message_id))
    if row is None and source == 'history':
        # WebSocket/ACK ids can differ from the decorated history id. Reconcile
        # only provisional local rows, with matching content/direction/time.
        candidates = db.scalars(select(DmHistory).where(DmHistory.account_id == account_id,
            DmHistory.peer_id == peer_id, DmHistory.source.in_(['live', 'receipt']),
            DmHistory.direction == direction, DmHistory.text == text,
            DmHistory.sent_at >= sent_at - timedelta(seconds=2),
            DmHistory.sent_at <= sent_at + timedelta(seconds=2)).order_by(DmHistory.id)).all()
        def urls(items):
            return [db.get(dm_media.DmMedia, item['asset_id']).url for item in items or []]
        row = next((candidate for candidate in candidates if candidate.content_type == content_type and urls(candidate.media) == urls(refs)), None)
    if row is None:
        row = DmHistory(account_id=account_id, peer_id=peer_id, message_id=message_id)
        db.add(row)
    # Platform history is authoritative (notably for revoked messages).
    if row.source == 'history' and source != 'history':
        return row
    row.media = refs
    row.message_id, row.direction, row.text = message_id, direction, text
    row.sent_at, row.source = sent_at, source
    row.content_type, row.revoked = content_type, revoked
    db.flush()
    return row


def empty_placeholder(raw):
    """An explicit type-0 record with no content is not a chat bubble."""
    if not isinstance(raw, dict) or raw.get('revoked'):
        return False
    body = raw.get('content')
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return False
    return (isinstance(body, dict) and type(body.get('content_type')) is int
            and body['content_type'] == 0 and body.get('content') == '')


def parse_message(raw, own_id, peer_id):
    if not isinstance(raw, dict) or raw.get('group_chat') or raw.get('groupChat'):
        return None
    sender, receiver = raw.get('sender_id'), raw.get('receiver_id')
    if (sender, receiver) not in ((own_id, peer_id), (peer_id, own_id)):
        return None
    if empty_placeholder(raw):
        return None
    sent_at = timestamp(raw.get('created_at'))
    mid = raw.get('id')
    if sent_at is None or not isinstance(mid, str) or not mid or len(mid) > 200:
        raise HistoryUnavailable('历史消息格式不完整，已暂停本轮分析并等待重试')
    content = raw.get('content')
    try:
        body = json.loads(content) if isinstance(content, str) else content
    except ValueError:
        body = content
    kind = body.get('content_type', 1) if isinstance(body, dict) else 1
    if type(kind) is not int:
        kind = 0
    revoked = bool(raw.get('revoked'))
    normalized = dm_content.normalize(body, kind) if not revoked else None
    text = '[已撤回]' if revoked else normalized['text'] if normalized else '[暂不支持的消息类型：' + str(kind) + ']'
    media = normalized['media'] if normalized else []
    return dict(message_id=mid, direction='outbound' if sender == own_id else 'inbound',
        text=text, content_type=kind, sent_at=sent_at, revoked=revoked, media=media)


def sync_history(db, account, peer_id, fetch_page, now=None, *, refresh_existing=False, reuse_if_covers=None, live_complete=False):
    now = now or shanghai_now()
    since = now - timedelta(hours=HISTORY_WINDOW_HOURS)
    record = conversation(db, account.id, peer_id)
    previous = dict(record.history_status or {})
    anchor, anchor_id = None, None
    if previous.get('complete') and not refresh_existing:
        try:
            old_since, old_until = (datetime.fromisoformat(previous[k]) for k in ('since', 'until'))
            if old_since <= since <= old_until <= now:
                anchor = old_until
                anchor_id = previous.get('max_store_id')
                # Only an active socket with completed recovery can extend
                # coverage from the persisted bidirectional live history.
                if live_complete:
                    bounded = {**previous, 'since': since.isoformat(), 'until': now.isoformat(),
                               'pages': 0, 'read_source': 'live_sql'}
                    record.history_status = bounded
                    db.commit()
                    return bounded
                if reuse_if_covers is not None and since <= reuse_if_covers <= anchor and now - anchor <= timedelta(seconds=30):
                    bounded = {**previous, 'window_hours': HISTORY_WINDOW_HOURS, 'since': since.isoformat(), 'read_source':'sql_cache'}
                    record.history_status = bounded
                    db.commit()
                    return bounded
        except (KeyError, TypeError, ValueError):
            pass
    max_store_id = previous.get('max_store_id', 0) if anchor is not None else 0
    cursor, pages, completed = 0, 0, False
    for _ in range(30):
        data = fetch_page(peer_id, last_id=cursor, limit=100)
        if not isinstance(data, dict) or not isinstance(data.get('out_message_list'), list):
            raise HistoryUnavailable('聊天历史接口返回异常，将自动重试')
        result = data.get('result')
        if not isinstance(result, dict) or result.get('success') is not True:
            raise HistoryUnavailable('聊天历史接口未返回成功，将自动重试')
        raw_messages = data['out_message_list']
        pages += 1
        if not raw_messages:
            completed = True
            break
        cursors, older = [], False
        for raw in raw_messages:
            # Pagination counts transport records, including empty placeholders.
            store_id = raw.get('store_id') if isinstance(raw, dict) else None
            if isinstance(store_id, int) and store_id > 0:
                cursors.append(store_id)
                max_store_id = max(max_store_id or 0, store_id)
                if anchor is not None and type(anchor_id) is int and store_id <= anchor_id:
                    older = True
            item = parse_message(raw, str(account.external_user_id), peer_id)
            if item is None:
                if empty_placeholder(raw) and (raw.get('sender_id'), raw.get('receiver_id')) in (
                        (str(account.external_user_id), peer_id), (peer_id, str(account.external_user_id))):
                    # Retain old misclassified rows for audit, but exclude them
                    # from chat history and correct any previously synced ledger.
                    previous_row = db.scalar(select(DmHistory).where(DmHistory.account_id == account.id,
                        DmHistory.peer_id == peer_id, DmHistory.message_id == raw.get('id'),
                        DmHistory.content_type == 0))
                    if previous_row is not None:
                        previous_row.source = 'placeholder'
                continue
            if anchor is not None and anchor_id is None and item['sent_at'] <= anchor:
                older = True
            if item['sent_at'] < since:
                older = True
                continue
            if item['sent_at'] <= now:
                save_message(db, account.id, peer_id, **item)
        db.commit()  # Do not hold SQLite's write lock while requesting the next page.
        if older or (cursors and min(cursors) == 1):
            completed = True
            break
        if not cursors:
            raise HistoryUnavailable('聊天历史分页缺少有效游标，将自动重试')
        next_cursor = min(cursors)
        # The API includes the last_id boundary again when the history is exhausted.
        if cursor and next_cursor == cursor and len(raw_messages) == 1:
            completed = True
            break
        if cursor and next_cursor >= cursor:
            raise HistoryUnavailable('聊天历史分页未向前推进，将自动重试')
        cursor = next_cursor
    if not completed:
        raise HistoryUnavailable('最近一天消息超过本轮分页上限，已暂停自动回复，未使用不完整历史')
    status = {**previous, 'window_hours':HISTORY_WINDOW_HOURS,'since':since.isoformat(),'until':now.isoformat(),
        'synced_at':now.isoformat(),'pages':pages,'complete':True,'max_store_id':max_store_id,'sync_requested':False,'read_source':'platform_sync'}
    record.history_status = status
    db.commit()
    return status


def history_rows(db, account_id, peer_id, since, until):
    return list(db.scalars(select(DmHistory).where(DmHistory.account_id == account_id,
        DmHistory.peer_id == peer_id, DmHistory.sent_at >= since, DmHistory.sent_at <= until,
        DmHistory.source != 'placeholder')
        .order_by(DmHistory.sent_at, DmHistory.id)))


def view(row):
    return {k: getattr(row, k) for k in ('message_id', 'direction', 'text', 'media', 'content_type', 'sent_at', 'revoked')}


def snapshot(db, account, row, status):
    messages = history_rows(db, account.id, row.peer_id,
        datetime.fromisoformat(status['since']), datetime.fromisoformat(status['until']))
    record = conversation(db, account.id, row.peer_id)
    context = dict(peer_id=row.peer_id, history=status,
        current_message={'message_id': row.message_id, 'text': row.text, 'media': row.media, 'received_at': row.created_at.isoformat()},
        messages=[{**view(m), 'sent_at': m.sent_at.isoformat(), 'role': 'self' if m.direction == 'outbound' else 'peer'} for m in messages],
        previous_analysis=record.analysis)
    context['history'] = {**status, 'total_messages': len(messages)}
    context = compact_context(context, maximum=21000)
    context['history']['included_messages'] = len(context['messages'])
    context['history']['context_truncated'] = bool(context.get('context_truncated'))
    return context
