"""XHS adapter for Channel's platform-independent session protocol.

Local history row identities survive native transport/history ID aliases.
Persist synchronization versions before I/O so a lost response is safe to retry.
"""
import hashlib
import json
from datetime import datetime
from backend.app.core.time import SHANGHAI_TZ
from backend.app.services import dm_history as history

RESERVED = {'session','history','messages','current_message','previous_analysis','memories','trigger_message_ids'}


def prepare(db, row, client):
    if row.channel_payload.get('session_snapshot_id'):
        return True
    account_id = row.channel_payload['account_id']
    conversation = history.conversation(db, row.account_id, row.peer_id)
    state = dict(conversation.session_state or {})
    if not state.get('id'):
        state['id'] = client.session(account_id, row.peer_id, surface='dm')['id']
        conversation.session_state = json.loads(json.dumps(state))
        db.commit()
    status = row.channel_payload['context']['history']
    rows = history.history_rows(db, row.account_id, row.peer_id,
        datetime.fromisoformat(status['since']), datetime.fromisoformat(status['until']))
    trigger_id = (row.receipt or {}).get('history_row_id')
    if not trigger_id:
        matches = [r for r in rows if r.message_id == row.message_id]
        if len(matches) != 1:
            raise history.HistoryUnavailable('无法可靠定位当前消息，请同步后重新分析')
        trigger_id = matches[0].id
    if trigger_id not in {r.id for r in rows}:
        raise history.HistoryUnavailable('当前消息不在已同步的历史范围内')
    cutoff = datetime.fromisoformat(status['since']).replace(tzinfo=SHANGHAI_TZ).timestamp() - 86400
    ledger = {k: dict(v) for k,v in state.get('messages', {}).items()
              if datetime.fromisoformat(v['sent_at']).timestamp() >= cutoff}
    changes = []
    for message in rows:
        key = 'xhs-history:' + str(message.id)
        old = ledger.get(key, {})
        stamp = old.get('sent_at') or message.sent_at.replace(tzinfo=SHANGHAI_TZ).isoformat()
        entry = dict(message_id=key, direction=message.direction, text=message.text,
                     sent_at=stamp, media=message.media or [], revoked=message.revoked)
        fingerprint = hashlib.sha256(json.dumps(entry,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        if old.get('digest') != fingerprint:
            old = dict(digest=fingerprint, revision=old.get('revision',0)+1, sent_at=stamp, acknowledged=False)
            ledger[key] = old
        if not old.get('acknowledged'):
            changes.append({**entry, 'revision':old['revision']})
    state['messages'] = ledger
    conversation.session_state = json.loads(json.dumps(state))
    db.commit()  # Intention is durable before any remote write.
    coverage = {k:datetime.fromisoformat(status[k]).replace(tzinfo=SHANGHAI_TZ).isoformat() for k in ('since','until')}
    coverage['complete'] = status['complete']
    chunks = [changes[i:i+200] for i in range(0,len(changes),200)] or [[]]
    for index, entries in enumerate(chunks):
        client.sync_session(account_id,state['id'],entries,coverage=coverage if index==len(chunks)-1 else None)
        for entry in entries:
            ledger[entry['message_id']]['acknowledged'] = True
        conversation.session_state = json.loads(json.dumps({**state,'messages':ledger}))
        db.commit()
    snapshot = client.session_snapshot(account_id,state['id'],row.channel_payload['message_id'],
        ['xhs-history:'+str(trigger_id)], reanalysis=bool((row.receipt or {}).get('analysis_attempts')))
    row.receipt = {**(row.receipt or {}),'session_snapshot':{
        k:snapshot[k] for k in ('id','session_id','revision','memory_revision','eligible','reason','context_bytes')}}
    if not snapshot['eligible']:
        row.state, row.error = 'skipped', snapshot['reason']
        db.commit()
        return False
    context = {**snapshot['context'], 'control_mode':row.channel_payload['context']['control_mode'], 'peer_id':row.peer_id}
    context['history'] = dict(context['history'])
    for key in ('since','until'):
        context['history'][key] = datetime.fromisoformat(context['history'][key]).astimezone(SHANGHAI_TZ).replace(tzinfo=None).isoformat()
    from backend.app.services import dm_dialogue
    context['dialogue_state'] = dm_dialogue.context(db, row, context)
    row.channel_payload = {**row.channel_payload, 'session_snapshot_id':snapshot['id'], 'context':context}
    from backend.app.services.dm_service import DM_INSTRUCTION, CONTROL_INSTRUCTIONS
    row.channel_payload['text'] = DM_INSTRUCTION.replace('当前用户消息：','当前用户消息见会话快照：') + CONTROL_INSTRUCTIONS[context['control_mode']]
    db.commit()
    return True


def request_payload(row):
    if not row.channel_payload.get('session_snapshot_id'):
        return row.channel_payload
    return {**row.channel_payload, 'context':{k:v for k,v in row.channel_payload['context'].items() if k not in RESERVED}}
