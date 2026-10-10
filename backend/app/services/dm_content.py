"""Normalize official IM content without interpreting chat data as instructions."""
import json

MEDIA_TYPES = {2: 'image', 7: 'sticker', 13: 'emoji', 16: 'sticker'}
SUPPORTED_TYPES = {1, *MEDIA_TYPES}
LABELS = {'image': '图片', 'sticker': '表情包', 'emoji': '表情'}


def unwrap(content):
    for _ in range(8):
        if isinstance(content, str):
            try:
                value = json.loads(content)
            except (ValueError, TypeError):
                break
            if isinstance(value, (dict, str)):
                content = value
            else:
                break
        elif isinstance(content, dict) and 'content' in content:
            content = content['content']
        else:
            break
    return content


def normalize(content, content_type=1):
    body = unwrap(content)
    if content_type == 1:
        text = body.get('text') if isinstance(body, dict) else body
        return {'text': text.strip(), 'media': [], 'content_type': 1} if isinstance(text, str) and text.strip() and len(text) <= 8000 else None
    if content_type not in MEDIA_TYPES:
        return None
    if not isinstance(body, dict):
        return None
    kind = MEDIA_TYPES[content_type]
    url = next((body[k] for k in ('url', 'link', 'image', 'imageUrl') if isinstance(body.get(k), str) and body[k]), '')
    if not url or len(url) > 2048:
        return None
    label = str(body.get('emojiKey') or LABELS[kind])[:120]
    # Only native media fields are retained. No arbitrary model-selected payload.
    native = {k: v for k, v in body.items() if k in ('url', 'link', 'image', 'imageUrl', 'smileId', 'smileType', 'width', 'height', 'size', 'isGif', 'subType', 'emojiKey', 'applySource', 'fireworkId', 'interactAnimType', 'operationInfo', 'playMode', 'thumb', 'assetLength')}
    if len(json.dumps(native, ensure_ascii=False)) > 16000:
        return None
    return {'text': '[' + label + ']', 'content_type': content_type,
            'media': [{'kind': kind, 'label': label, 'url': url, 'native': native, 'content_type': content_type}]}
