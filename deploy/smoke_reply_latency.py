"""Measure a real reply run with synthetic history and an isolated workbench DB."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import time
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from backend.app.core.database import Base, SessionLocal
from backend.app.core.time import shanghai_now
from backend.app.models import PlatformAccount, User
from backend.app.models.direct_message import DmListener
from backend.app.services import dm_service, dm_sessions
from backend.app.services.model_routing import channel_client, account_default
from backend.app.services.content_automation import Multica, PRIVATE, ROOT
from deploy.smoke_live_account_agents import BOUNDARY


def main():
    marker = 'synthetic-reply-latency-' + uuid.uuid4().hex[:12]
    folder = PRIVATE / marker
    folder.mkdir(mode=0o700)

    def save(name, data):
        path = folder / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + '\n')
        path.chmod(0o600)

    live = channel_client()
    remote = Multica(ROOT / 'channel/.local/multica.json')
    catalog = live.catalog()
    with SessionLocal() as production:
        real = production.scalar(select(PlatformAccount).where(
            PlatformAccount.platform == 'xhs', PlatformAccount.sub_type == 'pc',
            PlatformAccount.nickname == 'leo'))
        identity = {k: getattr(real, k) for k in ('id', 'user_id', 'external_user_id', 'nickname')}
    route, agent = account_default(catalog, 'dm', PlatformAccount(**identity))
    if agent['id'] != 'da72f33e-cac4-4ff0-a99d-d415219f3efc':
        raise ValueError('Leo reply route changed; test was not submitted')
    engine = create_engine('sqlite:///' + str(folder / 'isolated-workbench.sqlite3'))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        account = PlatformAccount(**identity, platform='xhs', sub_type='pc', status='active')
        db.add(User(id=account.user_id, username=marker, password_hash='unused'))
        db.add(account)
        db.commit()
        listener = DmListener(account_id=account.id, enabled=True, auto_reply=False,
                             control_mode='full_access', route_id=route['id'], agent_snapshot=agent,
                             started_at=shanghai_now() - timedelta(minutes=1))
        db.add(listener)
        db.commit()
        started = time.monotonic()
        started_at = shanghai_now().isoformat()
        row = dm_service.ingest(db, account, listener, {'chatMessage': {
            'messageid': marker, 'payload_json': {'sender': marker + '-peer',
            'receiver': account.external_user_id, 'content': '今天下班想去公园走十分钟，带瓶水就够了吗？',
            'contentType': 1, 'groupChat': False}}})
        dm_service.prepare_history(db, row, lambda *_a, **_kw: {
            'result': {'success': True}, 'out_message_list': []})
        if not dm_sessions.prepare(db, row, live):
            raise ValueError('Synthetic session snapshot was not eligible')
        row.channel_payload = {**row.channel_payload, 'text': row.channel_payload['text'] + BOUNDARY,
            'context': {**row.channel_payload['context'], 'synthetic_smoke': True,
                        'test_marker': marker, 'long_term_memory_writes': False}}
        db.commit()
        prepared = time.monotonic()
        dm_service.poll_channel(db, row, live)
        submitted = time.monotonic()
        save('submission.json', {'marker': marker, 'started_at': started_at,
             'event_id': row.channel_event_id, 'account_id': row.channel_payload['account_id'],
             'agent_id': agent['id'], 'real_social_sends': 0})
        print('Submitted real Leo reply test; isolated workbench, no social sender. Report:', folder, flush=True)
        deadline = started + 360
        progress_at = submitted
        while row.state == 'waiting' and time.monotonic() < deadline:
            time.sleep(0.5)
            dm_service.poll_channel(db, row, live)
            if time.monotonic() - progress_at >= 30:
                print('Waiting for AI:', round(time.monotonic() - started, 1), 'seconds', flush=True)
                progress_at = time.monotonic()
        ended = time.monotonic()
        status = live.status(row.channel_event_id, row.channel_payload['account_id'])
        execution = status.get('execution') or {}
        runs = remote.runs(execution['issue_id']) if execution.get('issue_id') else []
        run = next((r for r in runs if r['id'] == execution.get('task_id')), {})
        transcript = remote.transcript(run['id']) if run else []
        calls = [m for m in transcript if m.get('type') == 'tool_use']
        commands = [str(m.get('input') or '') for m in calls]
        comments = remote.comments(execution['issue_id']) if execution.get('issue_id') else []
        report = {'marker': marker, 'started_at': started_at, 'agent_id': agent['id'],
            'issue_id': execution.get('issue_id'), 'task_id': execution.get('task_id'),
            'scenario': 'one synthetic text DM; no reaction images; empty prior history',
            'prepare_seconds': round(prepared - started, 3),
            'submit_seconds': round(submitted - prepared, 3),
            'ai_and_return_seconds': round(ended - submitted, 3),
            'total_to_workbench_seconds': round(ended - started, 3),
            'workbench_state': row.state, 'channel_state': status['state'],
            'tool_calls': len(calls), 'batched_compose_used': any('compose.py' in c for c in commands),
            'bundled_input_used': any('reply_input.py' in c for c in commands),
            'agent_issue_comments': sum(c.get('author_type') == 'agent' for c in comments),
            'real_social_sends': 0, 'reply': row.reply_text,
            'reply_messages': (row.receipt or {}).get('reply_messages', [])}
        for field in ('created_at', 'started_at', 'completed_at'):
            report['run_' + field] = run.get(field)
        save('report.json', report)
        save('transcript.json', transcript)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if status['state'] != 'succeeded' or row.state != 'ready':
            raise SystemExit(2)


if __name__ == '__main__':
    main()
