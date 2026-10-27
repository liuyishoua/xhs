"""Resume already-authorized jobs after verified account recovery, once."""
import asyncio
import logging

from sqlalchemy import select, update
from backend.app.core.database import SessionLocal
from backend.app.models import PlatformAccount, PublishJob, User
from backend.app.api.publish import (
    _load_publish_options, _store_publish_options, publish_job_to_creator,
    get_creator_publish_adapter_factory,
)

log = logging.getLogger(__name__)


def recover():
    # A crash after reservation may have reached the platform. Do not replay.
    with SessionLocal() as db:
        db.execute(update(PublishJob).where(PublishJob.status == 'resuming').values(
            status='uncertain', publish_error='恢复发布时服务中断，先核对平台记录，不重复发送'))
        for job in db.scalars(select(PublishJob).where(PublishJob.status == 'publishing')):
            if _load_publish_options(job).get('login_recovery'):
                job.status, job.publish_error = 'uncertain', '发布请求中断，先核对平台记录，不重复发送'
        db.commit()


def run_once():
    with SessionLocal() as db:
        rows = db.scalars(select(PublishJob).where(PublishJob.status.in_(('waiting_login', 'pending'))).order_by(PublishJob.id)).all()
        for job in rows:
            account = db.get(PlatformAccount, job.platform_account_id)
            options = _load_publish_options(job)
            recovery = options.get('login_recovery') or {}
            initial = job.status == 'pending' and options.get('automatic_publish') is True and bool(options.get('multica_issue_id'))
            if job.status == 'pending' and not initial:
                continue
            if initial:
                recovery = {'external_id': account.external_user_id if account else '',
                    'account_id': job.platform_account_id, 'user_id': job.user_id, 'resume_attempts': 0}
            if (not account or account.user_id != job.user_id or account.sub_type != 'creator'
                or account.platform != job.platform or recovery.get('account_id') != account.id
                or recovery.get('user_id') != job.user_id or recovery.get('external_id') != account.external_user_id):
                job.status, job.publish_error = 'failed', '恢复发布的账号身份已变化，未发送'
                db.commit()
                continue
            if not initial and account.status != 'active':
                continue
            if recovery.get('resume_attempts') != 0:
                job.status, job.publish_error = 'failed', '本轮自动恢复已执行，未重复发送'
                db.commit()
                continue
            claimed = db.execute(update(PublishJob).where(PublishJob.id == job.id,
                PublishJob.status == job.status).values(status='resuming'))
            if claimed.rowcount != 1:
                db.rollback()
                continue
            if not initial:
                options['login_recovery'] = {**recovery, 'resume_attempts': 1}
            _store_publish_options(job, options)
            db.commit()
            try:
                publish_job_to_creator(job.id, current_user=db.get(User, job.user_id), db=db,
                                       adapter_factory=get_creator_publish_adapter_factory())
            except Exception:
                db.rollback()
                db.refresh(job)
                if job.status == 'resuming':
                    job.status, job.publish_error = 'failed', '恢复发布预检查未通过，稿件保留'
                    db.commit()
                # Known pre-send failures are recorded by the publisher. Never
                # automatically retry an ambiguous post request.
                log.warning('Publish recovery stopped for job %s, state %s', job.id, job.status)


async def run():
    await asyncio.to_thread(recover)
    while True:
        try:
            await asyncio.to_thread(run_once)
        except Exception:
            log.exception('Publish recovery iteration failed')
        await asyncio.sleep(5)
