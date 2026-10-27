"""One live WebSocket per enabled account; durable records survive service restart."""
import asyncio
import logging
import time
import json
from datetime import datetime, timedelta
from sqlalchemy import select, update
from backend.app.core.database import SessionLocal
from backend.app.core.security import decrypt_text
from backend.app.models import AccountCookieVersion, PlatformAccount
from backend.app.models.direct_message import DmListener, DirectMessage, DmManualMessage
from backend.app.services.account_service import cookie_header_from_text, decode_cookie_text, persist_refreshed_pc_cookies, record_session_event
from backend.app.services.account_health import AccountCheckError, require_pc_session
from backend.app.services import dm_service as service
from backend.app.services import dm_history as history
from backend.app.services import dm_content, dm_media
from backend.app.services import dm_chat
from backend.app.services import xhs_network
from backend.app.core.time import SHANGHAI_TZ, shanghai_now
from backend.app.adapters.xhs.im_adapter import XhsImConnection

log = logging.getLogger(__name__)


def recover():
    with SessionLocal() as db:
        db.execute(update(DmManualMessage).where(DmManualMessage.state=='sending')
            .values(state='uncertain',error='服务重启前发送结果未确认，不会自动重发'))
        for row in db.scalars(select(DirectMessage).where(DirectMessage.state == 'sending')):
            row.state = 'uncertain'
            row.error = '服务重启前发送结果未确认，请在小红书核对；不会自动重发'
            if row.receipt.get('reply_messages'):
                plan = [dict(item) for item in row.receipt['reply_messages']]
                index = row.receipt.get('reply_index', 0)
                plan[index]['status'] = 'uncertain'
                row.receipt = {**row.receipt, 'reply_messages': plan, 'reply_message': plan[index]}
        db.commit()


def enabled_accounts():
    with SessionLocal() as db:
        connected=set(db.scalars(select(DmListener.account_id).join(PlatformAccount,
            PlatformAccount.id == DmListener.account_id).where(
                DmListener.enabled.is_(True) | DmListener.manual_chat_enabled.is_(True))))
        connected.update(db.scalars(select(DmManualMessage.account_id).where(DmManualMessage.state.in_(['queued','sending']))))
        return list(connected)


def connection_data(account_id):
    with SessionLocal() as db:
        account = db.get(PlatformAccount, account_id)
        cookie = db.scalar(select(AccountCookieVersion).where(AccountCookieVersion.platform_account_id == account_id)
            .order_by(AccountCookieVersion.id.desc()))
        if not account or not cookie:
            raise ValueError('账号或登录 Cookie 不存在')
        return cookie_header_from_text(decrypt_text(cookie.encrypted_cookies)), account.external_user_id


def state(account_id, value, error=''):
    with SessionLocal() as db:
        listener = db.get(DmListener, account_id)
        if listener:
            listener.state = value
            listener.error = error
            if value=='listening':
                from backend.app.models import ApiLog
                last=db.scalar(select(ApiLog).where(ApiLog.endpoint=='internal/xhs/account-session',
                    ApiLog.meta['account_id'].as_integer()==account_id,ApiLog.meta['source'].as_string()=='dm_listener').order_by(ApiLog.id.desc()).limit(1))
                status='active' if value=='listening' else 'disconnected'
                account=db.get(PlatformAccount,account_id)
                if account and (not last or last.status!=status):
                    record_session_event(db,account,'dm_listener',status,component='listener.dm')
            db.commit()


def save_connection_cookies(account_id, original, refreshed, verified=False):
    with SessionLocal() as db:
        if not persist_refreshed_pc_cookies(db, account_id, original, refreshed, source='dm_websocket'):
            # PC comments and IM can independently rotate this request cookie.
            # Keep the newest stored value; never overwrite a concurrent login.
            encrypted = db.scalar(select(AccountCookieVersion.encrypted_cookies).where(
                AccountCookieVersion.platform_account_id == account_id).order_by(AccountCookieVersion.id.desc()).limit(1))
            current = decode_cookie_text(decrypt_text(encrypted)) if encrypted else None
            credentials = lambda value: {k: v for k, v in value.items() if k != 'acw_tc'}
            if current is None or credentials(current) != credentials(original) or credentials(current) != credentials(refreshed):
                raise ConnectionError('登录凭证已更新，重新加载账号会话')
        if verified:
            account = db.get(PlatformAccount, account_id)
            account.status, account.status_message = 'active', ''
        db.commit()


def account_failure(account_id, cookies, error):
    with SessionLocal() as db:
        latest = db.scalar(select(AccountCookieVersion).where(
            AccountCookieVersion.platform_account_id == account_id).order_by(AccountCookieVersion.id.desc()))
        if latest and decode_cookie_text(decrypt_text(latest.encrypted_cookies)) == decode_cookie_text(cookies):
            account = db.get(PlatformAccount, account_id)
            account.status, account.status_message = error.account_status, str(error)
            record_session_event(db, account, 'dm_websocket', error.account_status, cookie_version_id=latest.id,
                                 business_code=getattr(error, 'business_code', None))
            db.commit()


def receive(account_id, entry):
    with SessionLocal() as db:
        listener, account = db.get(DmListener, account_id), db.get(PlatformAccount, account_id)
        if listener and (listener.enabled or listener.manual_chat_enabled) and account:
            service.record_own_message(db, account, entry)
            incoming=service.parse_inbound(entry,str(account.external_user_id))
            if listener.enabled and not (incoming and dm_chat.controlled(db,account_id,incoming['peer_id'])):
                service.ingest(db, account, listener, entry)
            else:
                if incoming:
                    history.save_message(db,account_id,incoming['peer_id'],incoming['message_id'],'inbound',incoming['text'],
                        history.timestamp(incoming['timestamp']) or shanghai_now(),source='live',
                        content_type=incoming['content_type'],media=incoming['media']);db.commit()


def next_delivery(account_id, fetch_page=None, upload=None, settle_seconds=0, blocked_peers=(), live_complete=False):
    with SessionLocal() as db:
        listener = db.get(DmListener, account_id)
        if not listener or not listener.enabled:
            return None
        rows = db.scalars(select(DirectMessage).where(DirectMessage.account_id == account_id,
            DirectMessage.state.in_(['history_pending', 'history_error', 'queued', 'waiting', 'ready', 'approval_pending', 'sending', 'uncertain', 'held']))
            .order_by(DirectMessage.id)).all()
        blocked_peers = set(blocked_peers)
        blocked_peers.update(db.scalars(select(history.DmConversation.peer_id).where(
            history.DmConversation.account_id==account_id,
            history.DmConversation.session_state['manual_control'].as_boolean().is_(True))))
        for row in rows:
            if row.peer_id in blocked_peers:
                continue
            # Finish an in-flight Channel turn before advancing the conversation.
            newer = db.scalar(select(DirectMessage.id).where(DirectMessage.account_id == account_id,
                DirectMessage.peer_id == row.peer_id, DirectMessage.id > row.id).limit(1))
            legacy = not (row.channel_payload.get('context') or {}).get('history')
            if newer and (row.state in ('history_pending', 'history_error', 'queued', 'ready', 'approval_pending') or (legacy and row.state == 'held')):
                row.state, row.error = 'superseded', '已合并到该用户更新的消息，由模型结合聊天历史统一判断'
                db.commit()
                continue
            if legacy and fetch_page and row.state in ('queued', 'held', 'ready'):
                service.reset_analysis(db, row)
            if row.channel_payload.get('route_id') != listener.route_id and row.state in ('history_pending', 'history_error', 'queued', 'ready', 'approval_pending', 'held'):
                service.reset_analysis(db, row)
            policy = (row.channel_payload.get('context') or {}).get('control_mode')
            if policy and policy != listener.control_mode and row.state in ('queued', 'held', 'ready', 'approval_pending'):
                service.reset_analysis(db, row)
            if row.state in ('sending', 'uncertain', 'held'):
                blocked_peers.add(row.peer_id)
                continue
            if row.state in ('history_pending', 'history_error'):
                if row.state == 'history_pending' and row.created_at > shanghai_now() - timedelta(seconds=settle_seconds):
                    blocked_peers.add(row.peer_id)
                    continue
                if row.state == 'history_error' and row.updated_at > shanghai_now() - timedelta(seconds=30):
                    blocked_peers.add(row.peer_id)
                    continue
                try:
                    if fetch_page is None:
                        raise history.HistoryUnavailable('尚未连接聊天历史接口')
                    service.prepare_history(db, row, fetch_page, live_complete=live_complete)
                except Exception as exc:
                    db.rollback()
                    row = db.get(DirectMessage, row.id)
                    row.state = 'history_error'
                    row.error = str(exc) if isinstance(exc, history.HistoryUnavailable) else '聊天历史读取失败，30秒后自动重试；请检查 PC 登录状态或网络'
                    db.commit()
                    blocked_peers.add(row.peer_id)
                    continue
            if row.state in ('queued', 'waiting'):
                try:
                    service.poll_channel(db, row)
                except dm_media.MediaUnavailable as exc:
                    db.rollback()
                    row = db.get(DirectMessage, row.id)
                    row.state, row.error = 'history_error', str(exc) + '；30秒后重试'
                    db.commit()
                except Exception:
                    db.rollback()
                    row = db.get(DirectMessage, row.id)
                    row.error = 'Channel 暂时不可用，将保留原消息 ID 继续查询或提交'
                    db.commit()
            # A newer inbound may have arrived while the model was working.
            newer = db.scalar(select(DirectMessage.id).where(DirectMessage.account_id == account_id,
                DirectMessage.peer_id == row.peer_id, DirectMessage.id > row.id).limit(1))
            if newer and row.state in ('ready', 'approval_pending'):
                row.state, row.error = 'superseded', '生成期间收到更新消息，将结合最新聊天历史重新判断'
                db.commit()
                continue
            db.refresh(listener)
            if row.channel_payload.get('route_id') != listener.route_id and row.state in ('history_pending', 'history_error', 'queued', 'ready', 'approval_pending', 'held'):
                service.reset_analysis(db, row)
            policy = (row.channel_payload.get('context') or {}).get('control_mode')
            if policy and policy != listener.control_mode and row.state in ('ready', 'approval_pending', 'held', 'skipped'):
                service.reset_analysis(db, row)
                blocked_peers.add(row.peer_id)
                continue
            if row.state in ('skipped', 'failed', 'superseded'):
                continue
            if row.state == 'ready' and not (row.channel_payload.get('context') or {}).get('history', {}).get('complete'):
                if fetch_page:
                    service.reset_analysis(db, row)
                blocked_peers.add(row.peer_id)
                continue
            if row.state in ('ready', 'approval_pending') and fetch_page:
                from datetime import datetime
                from backend.app.models.direct_message import DmHistory
                snapshot_until = datetime.fromisoformat(row.channel_payload['context']['history']['until'])
                manual_reply = db.scalar(select(DmHistory.id).where(DmHistory.account_id == account_id,
                    DmHistory.peer_id == row.peer_id, DmHistory.direction == 'outbound',
                    DmHistory.source != 'placeholder',
                    DmHistory.message_id.not_in(service.sent_plan_ids(row)),
                    DmHistory.sent_at > snapshot_until).limit(1))
                if manual_reply or snapshot_until < shanghai_now() - timedelta(minutes=5):
                    if service.sent_plan_ids(row):
                        row.state, row.error = 'superseded', '本人已接话或本轮已过期，取消余下回复'
                        db.commit()
                    else:
                        service.reset_analysis(db, row)
                    blocked_peers.add(row.peer_id)
                    continue
            if row.state == 'ready' and listener.control_mode == 'request_approval' and (row.receipt or {}).get('approval', {}).get('message_id') != row.channel_payload['message_id']:
                row.state = 'approval_pending'
                db.commit()
            db.refresh(listener)
            if not listener.enabled:
                return None
            if row.state == 'ready' and listener.auto_reply:
                if not service.reply_is_due(row):
                    blocked_peers.add(row.peer_id)
                    continue
                if (row.receipt or {}).get('reply_message', {}).get('asset_id'):
                    if not upload:
                        blocked_peers.add(row.peer_id)
                        continue
                    try:
                        dm_media.prepare_outgoing(db, row, upload)
                    except Exception:
                        db.rollback()
                        row.error = '图片上传未完成，未发送；请重新分析或检查家庭网络'
                        row.state = 'failed'
                        db.commit()
                        continue
                return row.id
            blocked_peers.add(row.peer_id)
    return None


def finish(row_id, ack):
    with SessionLocal() as db:
        service.finish_send(db, row_id, ack)


def reconnect_peers(account_id, recent_peers=()):
    with SessionLocal() as db:
        rows = list(db.scalars(select(DirectMessage).where(DirectMessage.account_id == account_id)
                               .order_by(DirectMessage.id)))
        pending = list(dict.fromkeys(row.peer_id for row in rows if row.state in
            ('history_pending', 'history_error') or (row.state == 'queued' and
            not (row.channel_payload.get('context') or {}).get('history', {}).get('complete'))))
        requested=list(db.scalars(select(history.DmConversation.peer_id).where(history.DmConversation.account_id==account_id,
            history.DmConversation.history_status['sync_requested'].as_boolean().is_(True))))
        pending=list(dict.fromkeys(pending+requested))
        peers = set(pending) | set(recent_peers)
        return pending + sorted(peers - set(pending))


def reconnect_checkpoints(account_id):
    from backend.app.models.direct_message import DmConversation
    with SessionLocal() as db:
        listener = db.get(DmListener, account_id)
        cutoff = max(listener.started_at, shanghai_now() - timedelta(hours=history.HISTORY_WINDOW_HOURS))
        checkpoints = {row.peer_id: dict(row.history_status or {}) for row in db.scalars(
            select(DmConversation).where(DmConversation.account_id == account_id))}
        return checkpoints, cutoff


def record_reconnect_plan(account_id, connection, peers):
    from backend.app.models import ApiLog
    with SessionLocal() as db:
        account = db.get(PlatformAccount, account_id)
        dm_chat.save_profiles(db,account_id,getattr(connection,'recent_chat_summaries',{}))
        db.add(ApiLog(user_id=account.user_id, platform='xhs', endpoint='internal/xhs/dm-recovery-plan',
            status='planned', meta={'account_id': account_id,
                'summary_pages': getattr(connection, 'chat_summary_pages', 0),
                'catalog_conversations': len(getattr(connection, 'recent_chat_summaries', {})),
                'recovery_conversations': len(peers)}))
        db.commit()


def reconnect_history(account_id, fetch_page, recent_peers=(), *, only_peer=None, refresh_existing=False):
    """Recover post-enable text received during a disconnect, once per ID.

    Run before dispatch for this peer; the live reader buffers that peer while
    recovered messages enter in chronological order.
    """
    from backend.app.models.direct_message import DmHistory
    with SessionLocal() as db:
        listener, account = db.get(DmListener, account_id), db.get(PlatformAccount, account_id)
        if not listener or not (listener.enabled or listener.manual_chat_enabled) or not account:
            return 0
        peers = {only_peer} if only_peer is not None else set(db.scalars(
            select(DirectMessage.peer_id).where(DirectMessage.account_id == account_id).distinct())) | set(recent_peers)
        recovered = 0
        for peer in sorted(peers):
            db.refresh(listener)
            if not (listener.enabled or listener.manual_chat_enabled):
                break
            newest = db.scalar(select(DmHistory.sent_at).join(DirectMessage,
                (DirectMessage.account_id == DmHistory.account_id) &
                (DirectMessage.message_id == DmHistory.message_id)).where(
                DmHistory.account_id == account_id, DmHistory.peer_id == peer,
                DmHistory.direction == 'inbound').order_by(DmHistory.sent_at.desc()).limit(1))
            cutoff = max(listener.started_at, shanghai_now() - timedelta(hours=history.HISTORY_WINDOW_HOURS), newest or listener.started_at)
            status = history.sync_history(db, account, peer, fetch_page, refresh_existing=refresh_existing)
            for message in history.history_rows(db, account_id, peer, cutoff,
                    datetime.fromisoformat(status['until'])):
                if not listener.enabled:continue
                if dm_chat.controlled(db,account_id,peer):continue
                if message.direction != 'inbound' or message.content_type not in dm_content.SUPPORTED_TYPES or message.revoked:
                    continue
                if db.scalar(select(DirectMessage.id).where(DirectMessage.account_id == account_id,
                        DirectMessage.message_id == message.message_id)):
                    continue
                db.refresh(listener)
                if not listener.enabled:
                    break
                row = service.ingest(db, account, listener, {'chatMessage': {
                    'messageid': message.message_id,
                    'ts': int(message.sent_at.replace(tzinfo=SHANGHAI_TZ).timestamp() * 1000),
                    'payload_json': {'sender': peer, 'receiver': str(account.external_user_id),
                                     'content': (db.get(dm_media.DmMedia, message.media[0]['asset_id']).native if message.media else message.text), 'contentType': message.content_type}}})
                if row:
                    row.receipt = {**(row.receipt or {}), 'recovered_after_reconnect': True}
                    db.commit()
                    recovered += 1
        return recovered


def take_chat_requests(account_id):
    with SessionLocal() as db:
        row=db.get(DmListener,account_id)
        catalog=bool(row and row.chat_refresh_requested)
        if catalog:row.chat_refresh_requested=False;db.commit()
        requested=list(db.scalars(select(history.DmConversation.peer_id).where(
            history.DmConversation.account_id==account_id,
            history.DmConversation.history_status['sync_requested'].as_boolean().is_(True))))
        return catalog,requested


def prepare_manual(account_id, expected_user_id, upload):
    with SessionLocal() as db:
        row=dm_chat.next_message(db,account_id)
        if not row:return None
        try:
            if row.asset_id:dm_media.prepare_outgoing(db,row,upload)
        except Exception:
            row.state='failed';row.error='图片上传未完成，消息未发送';db.commit();return None
        row=dm_chat.claim(db,row.id,expected_user_id)
        if row:return (row.id,row.peer_id,row.text,row.outbound_mid,row.receipt.get('reply_message'))


def finish_manual(row_id, ack):
    with SessionLocal() as db:dm_chat.finish(db,row_id,ack)


async def deliver_manual(account_id, connection, pending, expected_user_id):
    delivery=await asyncio.to_thread(prepare_manual,account_id,expected_user_id,getattr(connection,'upload_image',None))
    if not delivery:return False
    row_id,peer,text,mid,reply=delivery
    pending[mid]=asyncio.get_running_loop().create_future();ack=None;auth_error=None
    try:
        xhs_network.require_ready()
        await connection.send(peer,text,mid,reply) if reply else await connection.send(peer,text,mid)
        ack=await asyncio.wait_for(pending[mid],timeout=20)
        if ack and ack.get('mid')==mid:
            require_pc_session(ack)
    except asyncio.CancelledError:raise
    except AccountCheckError as error:
        auth_error=error
        if ack is None and error.business_code in (-100,300012):
            ack={'mid':mid,'code':error.business_code,'msg':'登录未通过'}
    except Exception:pass
    finally:
        pending.pop(mid,None)
        await asyncio.shield(asyncio.to_thread(finish_manual,row_id,ack))
    if auth_error is not None:raise auth_error
    return True


async def account_loop(account_id):
    delay = 3
    invalid_cookies = None
    while True:
        connection = None
        cookies = None
        pending = {}
        reader = heartbeat = None
        try:
            xhs_network.require_ready()
            await asyncio.to_thread(state, account_id, 'connecting')
            cookies, user_id = await asyncio.to_thread(connection_data, account_id)
            if cookies == invalid_cookies:
                await asyncio.to_thread(state, account_id, 'reconnecting', '登录已失效，请在账号矩阵通过手机验证码重新登录；更新凭证后自动连接')
                await asyncio.sleep(30)
                continue
            invalid_cookies = None
            connection = XhsImConnection(cookies, user_id)
            await connection.connect()
            saved_cookies = decode_cookie_text(cookies)
            async def persist_session(verified=False):
                nonlocal saved_cookies, cookies
                refreshed = connection.cookie_snapshot()
                await asyncio.to_thread(save_connection_cookies, account_id, saved_cookies, refreshed, verified)
                saved_cookies = dict(refreshed)
                cookies = cookie_header_from_text(json.dumps(saved_cookies))
            await persist_session(verified=True)
            next_refresh = time.monotonic() + 600
            next_save = time.monotonic() + 25
            delay = 3
            buffered_entries = []
            recovery_pending = set()
            discovering_peers = True

            async def read():
                async for frame in connection.events():
                    entries = (frame.get('decoded') or {}).get('im', [])
                    if isinstance(entries, dict):
                        entries = [entries]
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        ack = entry.get('chatACK', {})
                        future = pending.get(ack.get('mid'))
                        if future and not future.done():
                            future.set_result(ack)
                        if 'chatMessage' in entry:
                            incoming = service.parse_inbound(entry, str(user_id))
                            peer = incoming['peer_id'] if incoming else None
                            if discovering_peers or peer in recovery_pending:
                                buffered_entries.append((peer, entry))
                            else:
                                await asyncio.to_thread(receive, account_id, entry)
                raise ConnectionError('私信连接已关闭')

            async def keepalive():
                while True:
                    await asyncio.sleep(25)
                    await connection.heartbeat()

            heartbeat = asyncio.create_task(keepalive())
            reader = asyncio.create_task(read())
            checkpoints, cutoff = await asyncio.to_thread(reconnect_checkpoints, account_id)
            recent = await asyncio.to_thread(connection.recent_peers, checkpoints=checkpoints, since=cutoff)
            recovery_queue = await asyncio.to_thread(reconnect_peers, account_id, recent)
            await asyncio.to_thread(record_reconnect_plan, account_id, connection, recovery_queue)
            recovery_pending.update(recovery_queue)
            discovering_peers = False
            retry_at = {}
            # Live traffic is read throughout recovery. A peer is dispatched only
            # after its complete history, without waiting for unrelated peers.
            await asyncio.to_thread(state, account_id, 'listening')
            while True:
                xhs_network.require_ready()
                if reader.done():
                    reader.result()
                if heartbeat.done():
                    heartbeat.result()
                if await deliver_manual(account_id,connection,pending,user_id):
                    await asyncio.sleep(0.5)
                    continue
                catalog,requested=await asyncio.to_thread(take_chat_requests,account_id)
                if catalog:
                    checkpoints,cutoff=await asyncio.to_thread(reconnect_checkpoints,account_id)
                    recent=await asyncio.to_thread(connection.recent_peers,checkpoints=checkpoints,since=cutoff)
                    requested=list(set(requested)|set(recent))
                    await asyncio.to_thread(record_reconnect_plan,account_id,connection,requested)
                for peer in requested:
                    if peer not in recovery_pending:
                        recovery_pending.add(peer);recovery_queue.insert(0,peer)
                due = next((peer for peer in recovery_queue if retry_at.get(peer, 0) <= time.time()), None)
                if due is not None:
                    recovery_queue.remove(due)
                    try:
                        await asyncio.to_thread(reconnect_history, account_id, connection.history_page, only_peer=due,
                            refresh_existing=due in getattr(connection, 'history_refresh_peers', set()))
                    except AccountCheckError:
                        raise
                    except Exception as exc:
                        recovery_queue.append(due)
                        retry_at[due] = time.time() + 30
                        log.warning('DM history retry: account=%s error_type=%s', account_id, type(exc).__name__)
                        with SessionLocal() as db:
                            db.execute(update(DirectMessage).where(DirectMessage.account_id == account_id,
                                DirectMessage.peer_id == due, DirectMessage.state.in_(['history_pending', 'history_error']))
                                .values(state='history_error', error='聊天历史读取失败，30秒后重试；其他会话继续处理'))
                            db.commit()
                    else:
                        recovery_pending.discard(due)
                # Keep buffering unrecovered peers to preserve their replay order.
                while True:
                    index = next((i for i, (peer, _) in enumerate(buffered_entries)
                                  if peer not in recovery_pending), None)
                    if index is None:
                        break
                    _, entry = buffered_entries.pop(index)
                    await asyncio.to_thread(receive, account_id, entry)
                if time.monotonic() >= next_refresh:
                    await asyncio.to_thread(connection.refresh_login)
                    await persist_session(verified=True)
                    next_refresh = time.monotonic() + 600
                if time.monotonic() >= next_save:
                    await persist_session()
                    next_save = time.monotonic() + 25
                delivery = await asyncio.to_thread(next_delivery, account_id, connection.history_page,
                    getattr(connection, 'upload_image', None), 0.5, blocked_peers=recovery_pending,
                    live_complete=True)
                if delivery:
                    xhs_network.require_ready()
                    # A tiny synchronous transaction keeps cancellation from abandoning
                    # a newly claimed send before the finally block owns its receipt.
                    with SessionLocal() as db:
                        current = db.get(DmListener, account_id)
                        claimed = service.claim_send(db, delivery) if current and current.enabled and current.auto_reply else None
                        if claimed:
                            network = xhs_network.status()
                            claimed.receipt = {**(claimed.receipt or {}), 'network': {
                                k: network[k] for k in ('mode', 'label', 'checked_at', 'exit_ip')}}
                            db.commit()
                        delivery = (claimed.id, claimed.peer_id, claimed.reply_text, claimed.outbound_mid, (claimed.receipt or {}).get('reply_message')) if claimed else None
                    if not delivery:
                        continue
                    row_id, peer, text, mid, reply = delivery
                    pending[mid] = asyncio.get_running_loop().create_future()
                    ack = None
                    try:
                        await connection.send(peer, text, mid, reply) if reply and reply.get('content_type') != 1 else await connection.send(peer, text, mid)
                        ack = await asyncio.wait_for(pending[mid], timeout=20)
                    except AccountCheckError as exc:
                        await asyncio.to_thread(account_failure,account_id,cookies,exc)
                    except Exception:
                        pass
                    finally:
                        pending.pop(mid, None)
                        await asyncio.shield(asyncio.to_thread(finish, row_id, ack))
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            raise
        except xhs_network.HomeNetworkUnavailable as exc:
            await asyncio.to_thread(state, account_id, 'network_paused', str(exc))
        except AccountCheckError as exc:
            await asyncio.to_thread(account_failure, account_id, cookies, exc)
            await asyncio.to_thread(state, account_id, 'reconnecting', str(exc))
            if exc.account_status == 'expired':
                invalid_cookies = cookies
        except Exception as exc:
            # Exceptions may contain URLs or authentication material. Show only a safe diagnosis.
            log.warning('DM connection retry: account=%s error_type=%s', account_id, type(exc).__name__)
            await asyncio.to_thread(state, account_id, 'reconnecting', '连接失败，正在重试；请检查 PC 账号登录状态或网络')
        finally:
            for task in (reader, heartbeat):
                if task:
                    task.cancel()
            await asyncio.gather(*(t for t in (reader, heartbeat) if t), return_exceptions=True)
            if connection:
                try:
                    await connection.close()
                except Exception:
                    pass
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


async def run():
    await asyncio.to_thread(recover)
    workers = {}
    try:
        while True:
            enabled = set(await asyncio.to_thread(enabled_accounts))
            for aid in list(workers):
                if workers[aid].done():
                    await asyncio.gather(workers.pop(aid), return_exceptions=True)
            for aid in set(workers) - enabled:
                workers[aid].cancel()
                await asyncio.gather(workers.pop(aid), return_exceptions=True)
                await asyncio.to_thread(state, aid, 'stopped')
            for aid in enabled - set(workers):
                workers[aid] = asyncio.create_task(account_loop(aid))
            await asyncio.sleep(2)
    finally:
        for task in workers.values():
            task.cancel()
        await asyncio.gather(*workers.values(), return_exceptions=True)
