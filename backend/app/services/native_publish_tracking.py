"""Track agent-owned publishing without invoking the legacy proposal pipeline."""
import json

from sqlalchemy import select

from backend.app.core.database import SessionLocal
from backend.app.models import PlatformAccount, PublishJob


def reconcile(executor, row):
    config = executor.config
    with SessionLocal() as db:
        account = db.get(PlatformAccount, config['creator_account_id'])
        if (not account or account.user_id != config['user_id']
                or account.sub_type != 'creator'
                or account.external_user_id != config['expected_external_user_id']):
            raise ValueError('自动发布跟踪账号不匹配')
        jobs = db.scalars(select(PublishJob).where(
            PublishJob.user_id == config['user_id'],
            PublishJob.platform_account_id == account.id,
            PublishJob.platform == 'xhs').order_by(PublishJob.id)).all()
        matches = [job for job in jobs if json.loads(job.publish_options or '{}').get('multica_issue_id') == row['issue_id']]
        if not matches:
            if row.get('job_id'):
                executor.ledger.update(row['id'], stage='stopped', detail='原发布记录已删除，不自动重建或重发')
                return
            if config.get('native_artifact_intake'):
                from backend.app.services.native_publish_artifacts import prepare
                job_id = prepare(executor, row)
                if job_id:
                    executor.ledger.update(row['id'], stage='native_waiting', job_id=job_id,
                        detail='已接收完整成稿和图片，发布与恢复由工作台继续')
                    return
            executor.ledger.update(row['id'], stage='native_waiting', detail='等待本任务关联的发布记录，不重复生成候选稿')
            return
        if len(matches) != 1:
            raise ValueError('同一任务关联多个发布记录，需核对，未自动重发')
        job = matches[0]
        note_id = job.external_note_id
        target = None
        stage = 'native_waiting'
        detail = job.publish_error or ''
        if job.status == 'published' and note_id:
            target, stage, detail = 'done', 'published', ''
        elif job.status in ('waiting_login', 'failed', 'uncertain'):
            target = 'blocked'
        elif job.status == 'cancelled':
            stage = 'stopped'
        payload = json.loads(row['payload'])
        payload['native_publish'] = {'job_id': job.id, 'status': job.status, 'note_id': note_id}
        payload['draft'] = {'title': job.title, 'photos': []}
        if stage == 'published':
            payload['receipt'] = {'success': True, 'note_id': note_id}
        job_id = job.id
    # The issue is checked against the configured agent before any status write.
    # Leave the ledger nonterminal until remote status reconciliation succeeds.
    if target:
        issue = executor.remote.issue(row['issue_id'])
        if (issue.get('assignee_id') != config['agent_id']
                or issue.get('assignee_type') != 'agent'):
            raise ValueError('任务负责人已变化，未修改任务状态')
        if issue['status'] == 'cancelled':
            stage = 'stopped'
        elif issue['status'] != target:
            executor.remote.request('PUT', '/api/issues/' + row['issue_id'],
                                    {'status': target, 'suppress_run': True})
    executor.ledger.update(row['id'], stage=stage, job_id=job_id,
                           external_id=note_id, payload=payload, detail=detail[:400])
