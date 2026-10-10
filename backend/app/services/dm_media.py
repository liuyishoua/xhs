"""Private account-owned image cache, visual inputs and outgoing media choices."""
import hashlib
import io
import json
import warnings
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from PIL import Image
from sqlalchemy import select
from backend.app.models.direct_message import DmMedia
from backend.app.services import xhs_network

ROOT = Path(__file__).resolve().parents[3] / '.local/dm-media'
LIMIT = 8 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 25_000_000

class MediaUnavailable(ValueError):
    pass


def safe_url(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        allowed = host.endswith('.xhscdn.com') or host == 'image-url-2-feature-1251524319.cos.ap-shanghai.myqcloud.com'
        if parsed.scheme not in ('http', 'https') or not allowed or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            raise ValueError()
        return urlunsplit(('https', host, parsed.path, parsed.query, ''))
    except (ValueError, TypeError):
        raise MediaUnavailable('图片地址不受支持，未读取图像内容') from None


def view(row):
    return {'asset_id': row.id, 'kind': row.kind, 'label': row.label,
            'content_type': row.content_type, 'library': row.library}


def register(db, account_id, media):
    refs = []
    for item in media or []:
        if 'asset_id' in item:
            row = db.get(DmMedia, item['asset_id'])
            if not row or row.account_id != account_id:
                raise MediaUnavailable('图片不属于当前账号')
        else:
            url = item['url']
            key = hashlib.sha256((str(account_id) + ':' + str(item['content_type']) + ':' + url + ':' + json.dumps(item['native'], sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
            row = db.get(DmMedia, key)
            if not row:
                row = db.scalar(select(DmMedia).where(DmMedia.account_id == account_id, DmMedia.url == url, DmMedia.content_type == item['content_type']).limit(1))
            if not row:
                row = DmMedia(id=key, account_id=account_id, kind=item['kind'], label=item['label'],
                    url=url, native=item['native'], content_type=item['content_type'])
                db.add(row); db.flush()
        refs.append(view(row))
    return refs


def validate_image(blob):
    if not blob or len(blob) > LIMIT:
        raise MediaUnavailable('图片最大支持 8MB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(blob)) as image:
                fmt = image.format
                if fmt not in ('PNG', 'JPEG', 'WEBP', 'GIF', 'BMP'):
                    raise ValueError()
                image.verify()
        return {'PNG': 'png', 'JPEG': 'jpg', 'WEBP': 'webp', 'GIF': 'gif', 'BMP': 'bmp'}[fmt], Image.MIME[fmt]
    except Exception:
        raise MediaUnavailable('图片格式无法读取或尺寸过大') from None


def store_blob(db, row, blob):
    ext, mime = validate_image(blob)
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = row.id + '.' + ext
    target = ROOT / filename
    temp = ROOT / (row.id + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_bytes(blob); temp.chmod(0o600); temp.replace(target)
    row.filename, row.mime = filename, mime
    db.commit()
    return target


def local_file(db, row):
    if row.filename and (ROOT / row.filename).is_file():
        return ROOT / row.filename
    from curl_cffi import requests
    xhs_network.require_ready()
    url = safe_url(row.url)
    # CDN receives no account cookies, and redirects cannot reach home LAN hosts.
    try:
        with requests.Session(trust_env=False) as session:
            response = session.get(url, proxies=xhs_network.http_options().get('proxies'), timeout=20,
                allow_redirects=False, stream=True)
            try:
                if response.status_code != 200:
                    raise MediaUnavailable('图片下载未完成，请稍后重试')
                chunks, size = [], 0
                for chunk in response.iter_content():
                    size += len(chunk)
                    if size > LIMIT:
                        raise MediaUnavailable('图片最大支持 8MB')
                    chunks.append(chunk)
                blob = b''.join(chunks)
            finally:
                response.close()
    except MediaUnavailable:
        raise
    except Exception:
        raise MediaUnavailable('图片下载未完成，请检查家庭网络后重试') from None
    return store_blob(db, row, blob)


def vision_blob(path):
    """Sample animation across its duration rather than claiming to view every frame."""
    with Image.open(path) as image:
        count = getattr(image, 'n_frames', 1)
        indices = sorted(set([0, count // 2, count - 1])) if count > 1 else [0]
        frames = []
        for i in indices:
            image.seek(i)
            frame = image.convert('RGB')
            frame.thumbnail((1000, 1000))
            frames.append(frame.copy())
        canvas = Image.new('RGB', (max(f.width for f in frames), sum(f.height for f in frames)), 'white')
        top = 0
        for frame in frames:
            canvas.paste(frame, (0, top)); top += frame.height
        canvas.thumbnail((1600, 2400))
        out = io.BytesIO(); canvas.save(out, 'JPEG', quality=88)
        return out.getvalue(), len(frames)


def select_visual_assets(row, context):
    """Read only images from this message and recent conversation history."""
    current = [ref['asset_id'] for ref in row.media or []]
    recent = [ref['asset_id'] for message in context.get('messages', [])[-6:]
              for ref in message.get('media', [])]
    return list(dict.fromkeys(current + list(reversed(recent))))[:8]


REACTION_CUES = (
    ('笑 哈哈 嘻嘻 乐 绷不住 笑死 好玩 逗', '偷笑 哈哈 开心 调皮 接梗'),
    ('累 难过 难受 委屈 辛苦 崩溃 哭', '抱抱 安慰 温柔'),
    ('晚安 困 睡', '晚安 睡觉 困了'),
    ('你好 早安 嗨 hello 拜拜', '你好 早安 打招呼 挥爪'),
    ('谢谢 过了 成功 太棒 终于 好消息', '赞 鼓励 加油 竖大拇指'),
    ('什么 竟然 震惊 离谱 问号 啊？', '震惊 惊讶 问号'),
    ('喜欢 可爱 夸 害羞 想你', '害羞 比心 喜欢'),
    ('斗图 表情包 表情 接招', '调皮 偷笑 哈哈 接梗'),
)


def select_reply_assets(db, row, context):
    """Offer local reviewed reactions without sacrificing current visual input."""
    conversation = select_visual_assets(row, context)
    current = list(dict.fromkeys(ref['asset_id'] for ref in row.media or []))[:8]
    recent = [key for key in conversation if key not in current]
    used = {ref['asset_id'] for message in context.get('messages', [])[-12:]
            if message.get('direction') == 'outbound' for ref in message.get('media', [])}
    text = ' '.join([row.text] + [m.get('text', '') for m in context.get('messages', [])[-3:]]).lower()
    candidates = db.scalars(select(DmMedia).where(DmMedia.account_id == row.account_id,
        DmMedia.library.is_(True), DmMedia.kind.in_(['sticker', 'emoji']), DmMedia.filename != '')
        .order_by(DmMedia.id).limit(80)).all()
    candidates = [a for a in candidates if Path(a.filename).name == a.filename and (ROOT / a.filename).is_file()
                  and a.id not in used and a.id not in conversation]
    def score(asset):
        return sum(sum(word in asset.label for word in labels.split())
                   for cues, labels in REACTION_CUES if any(word in text for word in cues.split()))
    candidates.sort(key=lambda a: (-score(a), a.id))
    # A small varied set is a proposal to the model, never an automatic send.
    library = []
    for asset in candidates:
        if asset.label not in {a.label for a in library}:
            library.append(asset)
        if len(library) == 4:
            break
    return list(dict.fromkeys(current + recent[:2] + [a.id for a in library] + recent[2:]))[:8]


def prepare_inputs(db, row, client):
    context = dict(row.channel_payload.get('context') or {})
    selected = select_reply_assets(db, row, context)
    images, catalog = [], []
    for key in selected:
        asset = db.get(DmMedia, key)
        if not asset or asset.account_id != row.account_id:
            raise MediaUnavailable('图片不属于当前账号')
        try:
            if not asset.channel_asset_id:
                blob, frames = vision_blob(local_file(db, asset))
                asset.channel_asset_id = client.upload(row.channel_payload['account_id'], blob, asset.id + '.jpg')['id']
                db.commit()
            images.append(asset.channel_asset_id)
            source = 'current' if key in {m['asset_id'] for m in row.media or []} else 'reaction_library' if asset.library else 'conversation'
            catalog.append({**view(asset), 'source': source, 'image_number': len(images), 'vision': '动态图片为首、中、末帧采样' if asset.mime == 'image/gif' else '图像附件'})
        except Exception:
            if key in {m['asset_id'] for m in row.media or []}:
                raise
            # Optional reactions and past pictures must not prevent a text
            # response when a cache file or attachment upload is unavailable.
            # Never put exception details (paths/HTTP credentials) in context.
            catalog.append({**view(asset), 'unavailable': '候选图片本轮未能读取，可用文字回应'})
    context['media_catalog'] = catalog
    context['media_prepared'] = True
    context['media_policy'] = '只根据实际附件描述图像；未在 catalog 中提供 image_number 的历史图片本轮未读取。动态图片为采样帧，不代表完整动画。可发送 catalog 中的 asset_id，或文字/颜文字/Unicode emoji；不得编造图片或提供外部地址。'
    row.channel_payload = {**row.channel_payload, 'context': context, **({'images': images} if images else {})}
    db.commit()  # Asset IDs and immutable input are durable before submitting.


def reply_message(db, row, structured):
    reply = structured.get('reply_message')
    if reply is None:
        reply = {'type': 'text', 'text': structured.get('reply_text')}
    if not isinstance(reply, dict):
        raise ValueError('回复格式无效')
    kind = reply.get('type')
    if kind in ('text', 'emoji', 'kaomoji') and isinstance(reply.get('text'), str) and reply['text'].strip() and len(reply['text']) <= 2000:
        return {'type': kind, 'text': reply['text'].strip(), 'content_type': 1, 'content': reply['text'].strip(), 'media': []}
    key = reply.get('asset_id')
    allowed = {m['asset_id'] for m in row.channel_payload.get('context', {}).get('media_catalog', []) if m.get('image_number')}
    if kind not in ('image', 'sticker', 'emoji') or not isinstance(key, str) or key not in allowed:
        raise ValueError('只能发送本轮已读取的账号图片或表情素材')
    asset = db.get(DmMedia, key)
    if not asset or asset.account_id != row.account_id:
        raise ValueError('图片不属于当前账号')
    if kind == 'sticker' and asset.content_type not in (7, 16) or kind == 'emoji' and asset.content_type != 13:
        raise ValueError('素材类型与回复类型不一致')
    native = dict(asset.native)
    if kind == 'image':
        # Existing platform pictures can be reused; uploaded assets get their
        # official CDN address in the transport preparation step before claiming.
        native = {'link': asset.url, 'localPath': '', 'assetLength': (ROOT / asset.filename).stat().st_size if asset.filename else 0,
                  'size': {'width': 0, 'height': 0}}
    return {'type': kind, 'asset_id': key, 'text': '[' + asset.label + ']',
        'content_type': 2 if kind == 'image' else asset.content_type,
        'content': json.dumps(native, ensure_ascii=False, separators=(',', ':')), 'media': [view(asset)]}


def reply_plan(db, row, structured):
    """Validate the entire burst before any bubble can be delivered."""
    proposals = structured.get('reply_messages')
    if proposals is None:
        proposals = [structured.get('reply_message') or {'type': 'text', 'text': structured.get('reply_text')}]
    if not isinstance(proposals, list) or not 1 <= len(proposals) <= 4:
        raise ValueError('回复应为1至4条消息；无需回复请选择 no_reply')
    plan = []
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, dict):
            raise ValueError('回复消息格式无效')
        delay = proposal.get('delay_seconds', 0 if index == 0 else 2)
        if type(delay) not in (int, float) or not 0 <= delay <= 15:
            raise ValueError('消息间隔应为0至15秒')
        reply = reply_message(db, row, {'reply_message': proposal})
        # Short, context-dependent pacing; no long unsolicited follow-up timers.
        plan.append({**reply, 'delay_seconds': delay, 'status': 'pending'})
    return plan


def prepare_outgoing(db, row, upload):
    reply = (row.receipt or {}).get('reply_message') or {}
    if not reply.get('asset_id'):
        return
    asset = db.get(DmMedia, reply['asset_id'])
    if not asset or asset.account_id != row.account_id:
        raise MediaUnavailable('图片不属于当前账号')
    if not asset.url:
        result = upload(local_file(db, asset))
        asset.url = result['url']
        asset.native = {'url': result['url'], 'smileId': result['file_id'], 'smileType': 0,
                        'width': result['width'], 'height': result['height'], 'isGif': int(asset.mime == 'image/gif')}
        db.commit()
    native = dict(asset.native)
    if reply['type'] == 'image':
        native = {'link': safe_url(asset.url), 'localPath': '', 'assetLength': (ROOT / asset.filename).stat().st_size if asset.filename else 0,
                  'size': {'width': native.get('width', 0), 'height': native.get('height', 0)}}
    updated = {**reply, 'content': json.dumps(native, ensure_ascii=False, separators=(',', ':'))}
    receipt = {**row.receipt, 'reply_message': updated}
    if receipt.get('reply_messages'):
        plan = [dict(item) for item in receipt['reply_messages']]
        plan[receipt.get('reply_index', 0)] = updated
        receipt['reply_messages'] = plan
    row.receipt = receipt
    db.commit()
