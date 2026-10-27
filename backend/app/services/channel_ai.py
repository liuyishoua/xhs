"""AI tasks reuse Channel's immutable routing and exact Multica run results."""
import base64
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, unquote
from backend.app.core.config import get_settings
from backend.app.services.model_routing import channel_client


def complete(config, text, *, images=None, category='content_analysis', account_id=None):
    client = channel_client()
    account = 'user:' + str(config.user_id)
    if config.base_url and config.base_url.startswith('account:'):
        from backend.app.core.database import SessionLocal
        from backend.app.models import PlatformAccount
        with SessionLocal() as db:
            owner = db.get(PlatformAccount, int(config.base_url.removeprefix('account:')))
            if not owner or owner.user_id != config.user_id or owner.platform != 'xhs':
                raise ValueError('任务绑定的账号已不可用，请重新选择 Agent')
            account = f'user:{owner.user_id}:account:{owner.external_user_id or owner.id}'
    if account_id is not None:
        account = str(account_id)
    assets = []
    for ref in images or []:
        if ref.startswith('data:image/') and ';base64,' in ref:
            blob = base64.b64decode(ref.split(';base64,', 1)[1], validate=True)
        elif ref.startswith('/api/files/media/'):
            name = unquote(ref.removeprefix('/api/files/media/'))
            if '/' in name or '\\' in name or name in ('', '.', '..'):
                raise ValueError('图片路径无效')
            if not name.startswith(tuple(f'xhs-{kind}-u{config.user_id}-' for kind in ('image', 'upload', 'asset'))):
                raise ValueError('图片不属于当前用户')
            path = Path(get_settings().storage_dir) / 'media' / name
            if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError('图片不存在或超过 8MB')
            blob = path.read_bytes()
        else:
            raise ValueError('Multica 图片任务请先将图片上传到素材库，暂不抓取外部图片链接')
        assets.append(client.upload(account, blob)['id'])
    event = client.send(account, 'ai-' + str(uuid.uuid4()), text, images=assets,
        category=category, route_id=config.model_name, new_task=True)
    deadline = time.monotonic() + 95
    while time.monotonic() < deadline:
        status = client.status(event['id'], account)
        if status['state'] == 'succeeded':
            result = status.get('result') or {}
            output = result.get('output') or {}
            if not output.get('text'):
                raise ValueError('Multica 未返回文本结果')
            structured = output.get('structured')
            if isinstance(structured, dict) and isinstance(structured.get('reply'), str):
                output = {**output, 'text': structured['reply']}
            return output
        if status['state'] in ('failed', 'failed_preflight', 'cancelled', 'blocked_no_route', 'blocked_reply', 'uncertain'):
            raise ValueError('Channel 任务需处理：' + status['state'] + '，消息 ID：' + event['id'])
        time.sleep(2)
    raise ValueError('Multica 仍在处理，消息 ID：' + event['id'] + '；请在 Channel 消息中枢查看原任务')


def image_result(config, prompt, references=None):
    output = complete(config, '请生成图片，完成后只输出 JSON 对象 {"url":"可访问的 HTTPS 图片地址"}。\n' + prompt,
                      images=references, category='image_analysis')
    parsed = output.get('structured')
    if not isinstance(parsed, dict) or not parsed.get('url'):
        try:
            parsed = json.loads(output['text'])
        except (ValueError, TypeError):
            parsed = {}
    url = parsed.get('url', '') if isinstance(parsed, dict) else ''
    if not isinstance(url, str) or urlsplit(url).scheme != 'https':
        raise ValueError('当前 Multica 路由未返回可访问的图片；请为图片用途配置具备图片生成能力的 agent')
    return {'url': url, 'raw': {'channel_output': output}}
