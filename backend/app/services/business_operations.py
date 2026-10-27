"""Precise, independently authorized workbench writes using existing adapters."""
import hashlib
import json
from sqlalchemy import select, update
from backend.app.models import PlatformAccount, PublishJob, PublishAsset
from backend.app.core.time import shanghai_now

class WriteUncertain(RuntimeError): pass

def assert_authority(ledger,p,body):
    with ledger.connect() as db: ledger.validate(db,p,body)

def reserve(ledger,p,body,target):
    with ledger.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        ledger.validate(db,p,body)
        db.execute('CREATE TABLE IF NOT EXISTS write_targets(principal TEXT,account TEXT,operation TEXT,target TEXT,request TEXT,PRIMARY KEY(principal,account,operation,target))')
        old=db.execute('SELECT request FROM write_targets WHERE principal=? AND account=? AND operation=? AND target=?',(p,str(body['account_id']),body['operation'],target)).fetchone()
        if old: raise ValueError('target already has a frozen write intent; reconcile instead of resend')
        db.execute('INSERT INTO write_targets VALUES(?,?,?,?,?)',(p,str(body['account_id']),body['operation'],target,body['request_id']))

def publish(db,ledger,p,body,account,user):
    from backend.app.api.publish import _get_latest_account_cookies, _asset_creator_upload_info, _apply_publish_options, _load_publish_options, _extract_external_note_id
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter
    args=body['input']
    if args.get('publish_job_id'): job=db.get(PublishJob,int(args['publish_job_id']))
    else: job=db.scalar(select(PublishJob).where(PublishJob.source_draft_id==int(args['draft_id']),PublishJob.user_id==user.id))
    creator=db.get(PlatformAccount,job.platform_account_id) if job else None
    if not job or job.user_id!=user.id or job.platform!='xhs' or not creator or creator.user_id!=user.id or creator.sub_type!='creator' or creator.platform!='xhs' or not account.external_user_id or creator.external_user_id!=account.external_user_id or creator.status!='active': raise ValueError('owned same-identity creator job required')
    if job.status not in ('pending','draft','ready') or job.publish_mode!='immediate' or not job.title.strip(): raise ValueError('only unsubmitted immediate image job supported')
    assets=db.scalars(select(PublishAsset).where(PublishAsset.publish_job_id==job.id)).all()
    if not assets or any(a.asset_type!='image' or a.upload_status!='uploaded' for a in assets): raise ValueError('all existing job images must be uploaded and validated first')
    info={'title':job.title,'desc':job.body,'media_type':'image','type':1,'image_file_infos':[_asset_creator_upload_info(a) for a in assets],'postTime':None}
    _apply_publish_options(info,_load_publish_options(job))
    adapter=XhsCreatorApiAdapter(_get_latest_account_cookies(db,creator.id))
    reserve(ledger,p,body,'publish-job:'+str(job.id))
    claim=db.execute(update(PublishJob).where(PublishJob.id==job.id,PublishJob.status.in_(('pending','draft','ready'))).values(status='publishing',publish_error=''))
    db.commit()
    if claim.rowcount!=1: raise ValueError('publish job already claimed')
    try:
        assert_authority(ledger,p,body)
        raw=adapter.post_note(info)
        external=_extract_external_note_id(raw)
        if not external: raise WriteUncertain('publication ID missing')
        job=db.get(PublishJob,job.id);job.status='published';job.external_note_id=external;job.published_at=shanghai_now();db.commit()
        return {'source':'authenticated_platform_submission','coverage':'submitted_image_note_not_visibility','account_id':account.id,'data':{'publish_job_id':job.id,'creator_account_id':creator.id,'external_id':external,'published_at':job.published_at.isoformat()}}
    except Exception:
        db.rollback(); job=db.get(PublishJob,job.id);job.status='uncertain';job.publish_error='Business task submission not confirmed; reconcile before retry';db.commit()
        raise WriteUncertain('publication not confirmed') from None

def comment(db,ledger,p,body,account,user,adapter):
    args=body['input']; note_id=str(args['note_id']); cid=str(args['target_comment_id']); text=str(args.get('text','')).strip()
    if not text or len(text)>2000: raise ValueError('comment text required within limits')
    # Reuse authentic mention identity/parent checks, never infer an incoming comment.
    from backend.app.services.content_automation import normalize_mention
    raw=adapter.get_mentions()
    items=raw.get('message_list') or raw.get('messages') or raw.get('items') or []
    match=None
    for item in items:
        managed={str(a.external_user_id) for a in db.scalars(select(PlatformAccount).where(PlatformAccount.user_id==user.id,PlatformAccount.platform=='xhs')).all() if a.external_user_id}
        normalized=normalize_mention(item,str(account.external_user_id),managed)
        if normalized and normalized['comment_id']==cid and normalized['note_id']==note_id: match=normalized;break
    if not match: raise ValueError('target not an authentic current incoming comment on our identity')
    reserve(ledger,p,body,'comment:'+note_id+':'+cid)
    assert_authority(ledger,p,body)
    try:
        result=adapter.post_comment(note_id,text,cid)
        data=result.get('data') or {}; comment=data.get('comment') or data
        external=str(comment.get('id') or comment.get('comment_id') or '')
        if not external: raise WriteUncertain('comment submission ID missing')
        return {'source':'authenticated_platform_submission','coverage':'submitted_comment_not_visibility','account_id':account.id,'data':{'note_id':note_id,'target_comment_id':cid,'external_id':external}}
    except Exception: raise WriteUncertain('comment write not confirmed') from None
