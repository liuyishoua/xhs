"""Push committed standard work orders; the same ApiLog is the durable outbox."""
from pathlib import Path
import sys
from sqlalchemy import event
from sqlalchemy.orm import Session
sys.path.insert(0,str(Path(__file__).resolve().parents[4]/'account-maintenance'))
from bridge import Bridge
DIRECTORY=Path(__file__).resolve().parents[3]/'.local'


def stage(db,account,row):
    from backend.app.core.database import engine
    if db.get_bind() is not engine:return
    bridge=Bridge(DIRECTORY)
    if not bridge.config or account.user_id!=int(bridge.config['user_id']):return
    meta=row.meta or {}
    if row.status=='active' and meta.get('source')!='scheduled_check':pass  # Status receipt only; the center updates an existing order.
    elif row.status in ('expired','risk') and meta.get('source') in ('dm_websocket','creator_identity_check','creator_upload','comment_identity_check','publisher_preflight'):pass
    else:return
    data={'schema_version':1,'source':'xhs','account_id':str(account.id),'external_id':account.external_user_id,
          'component':meta.get('component','account'),'reason_code':{'active':'connection_restored','expired':'auth_expired','risk':'verification_required','disconnected':'listener_disconnected'}[row.status],'recovery_method':'auto'}
    if type(meta.get('business_code')) is int:data['business_code']=meta['business_code']
    db.info.setdefault('maintenance_orders',[]).append((row,data))


@event.listens_for(Session,'after_commit')
def committed(db):
    for row,data in db.info.pop('maintenance_orders',[]):
        data['ticket_id']='xhs-log:'+str(row.id)
        Bridge(DIRECTORY).dispatch(data)


@event.listens_for(Session,'after_rollback')
def rolled_back(db):db.info.pop('maintenance_orders',None)
