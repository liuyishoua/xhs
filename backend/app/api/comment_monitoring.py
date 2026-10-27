"""Account-scoped monitoring and durable controls for comment replies."""
from __future__ import annotations
from contextlib import closing
import json
import sqlite3
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, StrictBool, Field
from sqlalchemy.orm import Session
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models import PlatformAccount, User
from backend.app.services.content_automation import (CONFIG, PRIVATE, TERMINAL, now, parse_time, dump,
    Ledger, comment_settings, comment_settings_key, comment_policy, comment_reply_version,
    comment_account_config, comment_ledger_path)
from backend.app.services.model_routing import channel_client, agent_choices, resolve_agent, account_route, account_default
from backend.app.services.channel_client import ChannelError
from backend.app.services.account_health import listener_account_issue

router = APIRouter(prefix='/xhs/comments', tags=['comment monitoring'])

class ListenerRequest(BaseModel):
    enabled: StrictBool
    auto_reply: StrictBool
    control_mode: Literal['request_approval', 'auto_approve', 'full_access']
    route_id: str = Field(default='', max_length=80)
    agent_id: str = Field(default='', max_length=80)


class ApprovalRequest(BaseModel):
    reply_version: str = Field(pattern=r'^[a-f0-9]{64}$')


def owned_config(account_id, user, db):
    account = db.get(PlatformAccount, account_id)
    if not account or account.user_id != user.id or account.platform != 'xhs' or account.sub_type != 'pc':
        raise HTTPException(404, 'Account not found')
    config = json.loads(CONFIG.read_text()) if CONFIG.is_file() else {}
    return account, comment_account_config(config, account) if config and account.external_user_id else None


def require_config(account_id, user, db):
    account, config = owned_config(account_id, user, db)
    if config is None:
        raise HTTPException(409, '此账号尚未配置评论自动化')
    return account, config


def state_reader(ledger):
    def read(key, default=None):
        row = ledger.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default
    return read


def writable_ledger(config):
    path = comment_ledger_path(config, PRIVATE)
    if not path.is_file() and not config.get('comment_only'):
        raise HTTPException(503, '评论执行器尚未初始化，请稍后重试')
    if not path.is_file():
        initialized = Ledger(path)
        initialized.db.close()
    return sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=3)


def available_comment_route(route):
    return bool(route.get('agent')) and all(
        '*' in route.get(key, []) or value in route.get(key, [])
        for key, value in [('categories', 'lifestyle_editor'), ('surfaces', 'manual'),
                           ('event_types', 'manual.request'), ('kinds', 'text')])


@router.get('/routes')
def routes(account_id: int = Query(..., gt=0), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    account, _ = require_config(account_id, current_user, db)
    try:
        catalog = channel_client().catalog()
        return {'connected': catalog.get('connected', True), 'agents': agent_choices(catalog, 'comment', account),
                'default_agent_id': '', 'routes': [
            {'id': r['id'], 'name': r['name'], 'agent_name': r['agent'].get('name', '')}
            for r in catalog['routes'] if available_comment_route(r) and account_route(r, account)]}
    except ChannelError:
        raise HTTPException(503, '评论回复路由暂时无法读取，请稍后重试') from None


@router.put('/listeners/{account_id}')
def configure(account_id: int, data: ListenerRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    account, config = require_config(account_id, current_user, db)
    # Stopping must remain available even when Channel is down.
    if data.enabled:
        if account.status != 'active':
            raise HTTPException(409, '请先恢复账号登录')
        try:
            catalog = channel_client().catalog()
            route, snapshot = (resolve_agent(catalog, 'comment', account, data.agent_id, data.route_id) if data.agent_id or data.route_id else account_default(catalog, 'comment', account))
        except ChannelError as exc:
            raise HTTPException(503, str(exc)) from None
    with closing(writable_ledger(config)) as ledger, ledger:
        ledger.execute('BEGIN IMMEDIATE')
        read = state_reader(ledger)
        before = comment_settings(config, read)
        settings = data.model_dump() if data.enabled else {**before, 'enabled': False}
        if data.enabled:
            settings.update(route_id=route['id'], agent_id=snapshot['id'], agent_snapshot=snapshot)
        settings.update(updated_at=now().isoformat(), updated_by=current_user.id)
        if config.get('comment_only'):
            settings['started_at'] = before.get('started_at') or (now().isoformat() if data.enabled else '')
        ledger.execute('INSERT OR REPLACE INTO state VALUES(?,?)', (comment_settings_key(config), dump(settings)))
        if data.enabled and not before['enabled']:
            ledger.execute('INSERT OR REPLACE INTO state VALUES(?,?)', ('comment_poll_at', '0'))
            health = {**read('comment_health', {}), 'state': 'starting', 'error': ''}
            ledger.execute('INSERT OR REPLACE INTO state VALUES(?,?)', ('comment_health', dump(health)))
    return monitoring(account_id, current_user, db)


def change_reply(account_id, entry_id, user, db, reply_version=None):
    _, config = require_config(account_id, user, db)
    with closing(writable_ledger(config)) as ledger, ledger:
        ledger.row_factory = sqlite3.Row
        ledger.execute('BEGIN IMMEDIATE')
        row = ledger.execute("SELECT * FROM entries WHERE id=? AND kind='comment'", (entry_id,)).fetchone()
        if not row or not entry_id.startswith('comment:' + str(account_id) + ':'):
            raise HTTPException(404, '评论不存在')
        if row['stage'] not in ('ready', 'approval_pending', 'held'):
            raise HTTPException(409, '评论状态已变化，请刷新后处理')
        payload = json.loads(row['payload'])
        if reply_version is not None:
            policy = comment_policy(comment_settings(config, state_reader(ledger)))
            if not (payload.get('reply') or {}).get('text') or payload.get('comment_policy') != policy or comment_reply_version(payload) != reply_version:
                raise HTTPException(409, '回复正文或配置已变化，请刷新后重新批准')
            payload['approval'] = {'reply_version': reply_version, 'approved_by': user.id, 'at': now().isoformat()}
            stage, detail = 'ready', '正文已批准，等待发送；发送开关关闭时保留草稿'
        else:
            stage, detail = 'no_reply', '账号主人手动跳过此条回复'
        ledger.execute('UPDATE entries SET stage=?,payload=?,detail=?,updated=? WHERE id=?',
                       (stage, dump(payload), detail, now().isoformat(), entry_id))
    return {'ok': True}


@router.post('/listeners/{account_id}/entries/{entry_id}/approve')
def approve(account_id: int, entry_id: str, data: ApprovalRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return change_reply(account_id, entry_id, current_user, db, data.reply_version)


@router.post('/listeners/{account_id}/entries/{entry_id}/skip')
def skip(account_id: int, entry_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return change_reply(account_id, entry_id, current_user, db)

@router.get('/monitoring')
def monitoring(account_id: int = Query(..., gt=0), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    account, config = owned_config(account_id, current_user, db)
    issue = listener_account_issue(account.status)
    result = {'account_id': account.id, 'nickname': account.nickname, 'configured': False, 'items': [], 'schedule': [],
              'account_status': account.status, 'account_problem': issue['message'] if issue else ''}
    if config is None: return result
    result.update(configured=True, automation_enabled=True if config.get('comment_only') else bool(config.get('enabled')), poll_seconds=config.get('comment_poll_seconds', 60),
                  **comment_settings(config, lambda key, default: default))
    path = comment_ledger_path(config, PRIVATE)
    if not path.is_file(): return {**result, 'state': 'stopped' if not result['enabled'] else issue['state'] if issue else 'starting'}
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as ledger:
        ledger.row_factory = sqlite3.Row
        state = state_reader(ledger)
        result.update(comment_settings(config, state))
        health = state('comment_health', {})
        heartbeat = state('heartbeat'); execution = state('execution', 'starting')
        stale = not heartbeat or (now() - parse_time(heartbeat)).total_seconds() > 120
        checked_at = health.get('checked_at')
        poll_stale = not checked_at or (now() - parse_time(checked_at)).total_seconds() > max(180, config.get('comment_poll_seconds', 60) * 3)
        status = ('stopped' if not result['enabled'] else 'paused' if not result['automation_enabled'] or execution == 'paused' else
                  'stale' if stale or poll_stale else health.get('state', 'starting'))
        if status == 'stale' and (not heartbeat or health.get('state') == 'starting') and result.get('updated_at') and (now() - parse_time(result['updated_at'])).total_seconds() < 120:
            status = health.get('state', 'starting')
        if result['enabled'] and result['automation_enabled'] and execution != 'paused':
            if issue:
                status = issue['state']
            elif status in ('login_expired', 'account_unavailable'):
                status = 'starting'
        counts = {r['stage']: r['n'] for r in ledger.execute("SELECT stage,count(*) n FROM entries WHERE kind='comment' AND detail NOT LIKE '历史验收记录：%' GROUP BY stage")}
        items = []
        for row in ledger.execute("SELECT * FROM entries WHERE kind='comment' AND detail NOT LIKE '历史验收记录：%' ORDER BY created DESC LIMIT 100"):
            payload = json.loads(row['payload']); decision = payload.get('decision') or {}
            verification = payload.get('verification') or {}
            display_stage = 'submitted' if row['stage'] == 'replied' and verification.get('state') != 'visible' else row['stage']
            items.append({'id': row['id'], 'stage': display_stage, 'text': payload.get('text', ''),
                'target_text': payload.get('target_text', ''), 'note_title': payload.get('note_title', ''),
                'note_id': payload.get('note_id', ''), 'comment_id': payload.get('comment_id', ''),
                'peer_id': payload.get('peer_id', ''), 'kind': payload.get('kind', ''),
                'reply_text': (payload.get('reply') or {}).get('text', ''), 'reply_comment_id': row['external_id'],
                'reason': decision.get('reason', ''), 'detail': row['detail'],
                'agent_snapshot': payload.get('agent_snapshot', {}),
                'reply_version': comment_reply_version(payload),
                'verification': {k: verification.get(k) for k in ('state', 'checked_at', 'observer_role', 'sender_visible', 'observer_visible')},
                'created_at': row['created'], 'updated_at': row['updated']})
        visible_count = ledger.execute("SELECT count(*) FROM entries WHERE kind='comment' AND stage='replied' AND detail NOT LIKE '历史验收记录：%' AND json_extract(payload,'$.verification.state')='visible'").fetchone()[0]
        delivery = state('comment_delivery', {})
        result.update(verified_visible=visible_count, sending_paused=bool(delivery.get('blocked')), sending_pause_reason=delivery.get('reason', '') if delivery.get('blocked') else '', visibility_warning=delivery.get('reason', '') if delivery.get('has_visibility_issue') else '', verification_error=state('comment_verify_error'), state=status, heartbeat=heartbeat, checked_at=checked_at, error='' if issue or health.get('state') in ('login_expired', 'account_unavailable') else health.get('error', ''),
            eligible_notifications=health.get('eligible_notifications', 0), counts=counts,
            pending=sum(n for stage, n in counts.items() if stage not in TERMINAL), items=items,
            schedule=state('schedule_window', {}).get('days', []), schedule_error=bool(state('schedule_error') or state('schedule_window', {}).get('pending_labels')))
    return result
