"""Persist completed publishing-agent attachments before platform preflight."""
from io import BytesIO
import json
import os
from pathlib import Path
from uuid import UUID

from PIL import Image
import requests
from sqlalchemy import select

from backend.app.core.database import SessionLocal
from backend.app.models import AiDraft, PublishAsset, PublishJob

ROOT = Path(__file__).resolve().parents[3] / '.local/native-publish-assets'


def download(remote, attachment):
    ident = str(UUID(attachment['id']))
    with requests.Session() as session:
        session.trust_env = False
        with session.get(remote.base + '/api/attachments/' + ident + '/download',
                headers={'Authorization': 'Bearer ' + remote.config['token'],
                         'X-Workspace-ID': remote.workspace},
                timeout=(5, 30), allow_redirects=False, stream=True) as response:
            response.raise_for_status()
            if response.is_redirect:
                raise ValueError('附件下载发生重定向，未接收')
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > 8 * 1024 * 1024:
                    raise ValueError('发布附件超过8MB')
            return bytes(data)


def prepare(executor, row):
    """Only completed, assigned-agent output on this exact issue is eligible."""
    config = executor.config
    issue = executor.remote.issue(row['issue_id'])
    if (issue.get('assignee_type') != 'agent' or issue.get('assignee_id') != config['agent_id']
            or issue['status'] in ('cancelled', 'done')):
        return None
    tasks = executor.remote.runs(row['issue_id'])
    tasks = [task for task in tasks if task.get('agent_id') == config['agent_id']]
    tasks.sort(key=lambda task: task.get('created_at', ''), reverse=True)
    if not tasks or tasks[0].get('status') != 'completed':
        return None
    attachments = [a for a in issue.get('attachments', [])
                   if a.get('uploader_type') == 'agent' and a.get('uploader_id') == config['agent_id']]
    notes = [a for a in attachments if a.get('filename') == 'note.md']
    if len(notes) != 1:
        return None
    # Only final images attached together with the completed note are accepted.
    images = [a for a in attachments if a.get('content_type', '').startswith('image/')
              and a.get('comment_id') and a.get('comment_id') == notes[0].get('comment_id')]
    if not 1 <= len(images) <= 9:
        return None
    text = download(executor.remote, notes[0]).decode('utf-8').strip()
    title, separator, body = text.partition('\n\n')
    title = title.lstrip('# ').strip()
    if not separator or not 1 <= len(title) <= 20 or not body.strip() or len(body) > 10000:
        raise ValueError('成稿附件格式无效，需标题和正文')
    directory = ROOT / str(UUID(row['issue_id']))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = []
    for attachment in images:
        raw = download(executor.remote, attachment)
        with Image.open(BytesIO(raw)) as image:
            if image.format not in ('PNG', 'JPEG', 'WEBP') or image.width * image.height > 12_000_000:
                raise ValueError('发布图片格式或尺寸不支持')
            image.verify()
        path = directory / (str(UUID(attachment['id'])) + '.jpg')
        with Image.open(BytesIO(raw)) as image:
            image.convert('RGB').save(path, format='JPEG', quality=92)
        os.chmod(path, 0o600)
        paths.append(str(path))
    with SessionLocal() as db:
        for existing in db.scalars(select(PublishJob).where(PublishJob.user_id == config['user_id'])):
            if json.loads(existing.publish_options or '{}').get('multica_issue_id') == row['issue_id']:
                return existing.id
        draft = AiDraft(user_id=config['user_id'], platform='xhs', title=title, body=body.strip())
        db.add(draft)
        db.flush()
        job = PublishJob(user_id=config['user_id'], platform='xhs',
            platform_account_id=config['creator_account_id'], source_draft_id=draft.id,
            title=title, body=body.strip(), status='pending',
            publish_options=json.dumps({'privacy_type': 0, 'multica_issue_id': row['issue_id'],
                'automatic_publish': True, 'attachment_ids': [a['id'] for a in [notes[0], *images]]}))
        db.add(job)
        db.flush()
        for path in paths:
            db.add(PublishAsset(publish_job_id=job.id, asset_type='image', file_path=path))
        db.commit()
        return job.id
