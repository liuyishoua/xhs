"""Human-authored chat, isolated from model generation and replay-safe."""
import uuid
from datetime import timedelta
from sqlalchemy import select, update, func
from fastapi import HTTPException
from backend.app.core.time import shanghai_now
from backend.app.models.direct_message import DmListener, DmManualMessage, DmHistory, DmMedia, DirectMessage, DmConversation
from backend.app.services import dm_history as history, dm_media

def listener(db, account):
    row=db.get(DmListener,account.id)
    if row is None:
        row=DmListener(account_id=account.id,enabled=False,auto_reply=False)
        db.add(row);db.flush()
    return row

def view(row):
    return {'id':row.id,'client_id':row.client_id,'account_id':row.account_id,'peer_id':row.peer_id,
        'text':row.text,'asset_id':row.asset_id,'state':row.state,'error':row.error,
        'created_at':row.created_at.isoformat(),'receipt':{k:row.receipt.get(k) for k in ('mid','messageid','code','msg')}}

def queue(db, account, client_id, peer_id, text, asset_id=None):
    if peer_id==account.external_user_id:
        raise HTTPException(400,'请选择其他用户聊天')
    connection=listener(db,account)
    # Take SQLite's write lock before checking simultaneous automatic sends.
    db.execute(update(DmListener).where(DmListener.account_id==account.id).values(updated_at=shanghai_now()))
    old=db.scalar(select(DmManualMessage).where(DmManualMessage.account_id==account.id,DmManualMessage.client_id==client_id))
    text=text.strip()
    if old:
        if (old.peer_id,old.text,old.asset_id)!=(peer_id,text or '[图片]',asset_id):
            raise HTTPException(409,'同一发送请求的内容不能更改')
        db.rollback();return old
    if bool(text)==bool(asset_id):
        raise HTTPException(400,'每次发送一条文字或一张图片')
    asset=db.get(DmMedia,asset_id) if asset_id else None
    if asset_id and (not asset or asset.account_id!=account.id):
        raise HTTPException(404,'图片不属于当前账号')
    busy=db.scalar(select(DirectMessage.id).where(DirectMessage.account_id==account.id,
        DirectMessage.peer_id==peer_id,DirectMessage.state=='sending').limit(1))
    if busy:
        raise HTTPException(409,'该会话正在发送回复，请稍后再试')
    connection.manual_chat_enabled=True
    receipt={'reply_message':{'type':'image','asset_id':asset_id,'content_type':2}} if asset_id else {}
    row=DmManualMessage(account_id=account.id,external_user_id=account.external_user_id,
        client_id=client_id,peer_id=peer_id,text=text or '[图片]',
        asset_id=asset_id,outbound_mid=str(uuid.uuid4()),state='queued',receipt=receipt)
    db.add(row)
    db.execute(update(DirectMessage).where(DirectMessage.account_id==account.id,DirectMessage.peer_id==peer_id,
        DirectMessage.state.in_(['history_pending','history_error','queued','waiting','ready','approval_pending','held']))
        .values(state='skipped',error='本人已接话，取消本轮自动回复'))
    # Sending once cancels stale drafts, but only the explicit control action
    # changes whether future inbound messages receive automatic replies.
    db.commit();db.refresh(row);return row

def next_message(db, account_id):
    candidates=db.scalars(select(DmManualMessage).where(DmManualMessage.account_id==account_id,
        DmManualMessage.state.in_(['queued','sending','uncertain'])).order_by(DmManualMessage.id)).all()
    blocked=set()
    for row in candidates:
        if row.peer_id in blocked:continue
        if row.state!='queued':blocked.add(row.peer_id);continue
        return row

def claim(db, row_id, expected_user_id):
    row=db.get(DmManualMessage,row_id)
    if not row:return None
    from backend.app.models import PlatformAccount
    account=db.get(PlatformAccount,row.account_id)
    if not account or account.external_user_id!=row.external_user_id or row.external_user_id!=expected_user_id:
        row.state='failed';row.error='账号连接已变化，消息未发送';db.commit();return None
    if row.receipt.get('proactive_item_id'):
        from backend.app.services.proactive_dm import permit
        if not permit(db,row,account):return None
    if db.scalar(select(DirectMessage.id).where(DirectMessage.account_id==row.account_id,
        DirectMessage.peer_id==row.peer_id,DirectMessage.state=='sending').limit(1)):return None
    result=db.execute(update(DmManualMessage).where(DmManualMessage.id==row_id,DmManualMessage.state=='queued')
        .values(state='sending',error='',receipt={**row.receipt,'send_started_at':shanghai_now().isoformat()}))
    db.commit()
    if result.rowcount!=1:return None
    db.refresh(row);return row

def finish(db, row_id, ack=None):
    row=db.get(DmManualMessage,row_id)
    if not row or row.state!='sending':return
    matched=bool(ack and ack.get('mid')==row.outbound_mid)
    if matched:row.receipt={**row.receipt,**{k:ack.get(k) for k in ('mid','messageid','code','msg','ts')}}
    if matched and type(ack.get('code')) is int and ack['code']==0 and ack.get('msg')=='ok':
        row.state='sent';row.error=''
        media=[dm_media.view(db.get(DmMedia,row.asset_id))] if row.asset_id else []
        saved=history.save_message(db,row.account_id,row.peer_id,ack.get('messageid') or row.outbound_mid,
            'outbound',row.text,history.timestamp(ack.get('ts')) or shanghai_now(),source='receipt',
            content_type=2 if row.asset_id else 1,media=media)
        row.receipt={**row.receipt,'history_row_id':saved.id}
    elif matched and ack.get('code') in (-100, 300012) and row.receipt.get('proactive_item_id'):
        row.state='queued';row.error='平台明确拒绝登录，等待账号恢复后续发'
    elif matched and type(ack.get('code')) is int and (ack['code']!=0 or (isinstance(ack.get('msg'),str) and ack['msg'] and ack['msg']!='ok')):
        row.state='failed';row.error='平台拒绝发送，业务码 '+str(ack['code'])
    else:
        row.state='uncertain';row.error='发送结果待核对，不会自动重发'
    db.commit()

def save_profiles(db, account_id, summaries):
    for peer,summary in summaries.items():
        row=history.conversation(db,account_id,peer)
        profile={k:summary.get(k) for k in ('nickname','avatar','last_msg_time')}
        if row.history_status.get('peer_profile')!=profile:
            row.history_status={**row.history_status,'peer_profile':profile}
    db.commit()

def controlled(db,account_id,peer_id):
    row=db.scalar(select(DmConversation).where(DmConversation.account_id==account_id,DmConversation.peer_id==peer_id))
    return bool(row and row.session_state.get('manual_control'))

def set_control(db,account_id,peer_id,enabled):
    db.execute(update(DmListener).where(DmListener.account_id==account_id).values(updated_at=shanghai_now()))
    if not enabled and db.scalar(select(DmManualMessage.id).where(DmManualMessage.account_id==account_id,
        DmManualMessage.peer_id==peer_id,DmManualMessage.state.in_(['queued','sending','uncertain'])).limit(1)):
        raise HTTPException(409,'请先完成或核对待发送消息')
    record=history.conversation(db,account_id,peer_id)
    record.session_state={**record.session_state,'manual_control':enabled}
    db.execute(update(DirectMessage).where(DirectMessage.account_id==account_id,DirectMessage.peer_id==peer_id,
        DirectMessage.state.in_(['history_pending','history_error','queued','waiting','ready','approval_pending','held']))
        .values(state='skipped',error='本人已接管会话，旧自动回复不补发'))
    db.commit()

def conversations(db, account_id):
    rows=db.scalars(select(DmConversation).where(DmConversation.account_id==account_id)).all()
    items=[]
    for row in rows:
        last=db.scalar(select(DmHistory).where(DmHistory.account_id==account_id,DmHistory.peer_id==row.peer_id,
            DmHistory.source!='placeholder')
            .order_by(DmHistory.sent_at.desc(),DmHistory.id.desc()).limit(1))
        profile=row.history_status.get('peer_profile') or {}
        stamp=last.sent_at if last else history.timestamp(profile.get('last_msg_time'))
        items.append({'peer_id':row.peer_id,'nickname':profile.get('nickname') or row.peer_id,
            'avatar':profile.get('avatar') or '', 'last_text':last.text if last else '',
            'last_at':stamp.isoformat() if stamp else None,'syncing':bool(row.history_status.get('sync_requested'))})
    return sorted(items,key=lambda r:r['last_at'] or '',reverse=True)

def timeline(db, account_id, peer_id):
    now=shanghai_now();since=now-timedelta(hours=history.HISTORY_WINDOW_HOURS)
    messages=history.history_rows(db,account_id,peer_id,since,now)
    items=[{'key':'history:'+str(m.id),**history.view(m),'state':'received' if m.direction=='inbound' else 'sent'} for m in messages]
    mids={m.message_id for m in messages}
    history_ids={m.id for m in messages}
    for row in db.scalars(select(DmManualMessage).where(DmManualMessage.account_id==account_id,
        DmManualMessage.peer_id==peer_id,DmManualMessage.created_at>=since)):
        if row.receipt.get('messageid') in mids or row.receipt.get('history_row_id') in history_ids:continue
        media=[dm_media.view(db.get(DmMedia,row.asset_id))] if row.asset_id else []
        items.append({'key':'manual:'+row.client_id,'manual_id':row.id,'message_id':row.outbound_mid,'client_id':row.client_id,
            'direction':'outbound','text':row.text,'sent_at':row.created_at.isoformat(),'state':row.state,
            'error':row.error,'content_type':2 if row.asset_id else 1,'media':media,'revoked':False})
    return sorted(items,key=lambda r:r['sent_at'])
