"""Collect public reaction GIFs and import explicitly reviewed files. No sends."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote, urlsplit
import requests

QUERIES = [
    ('cat laugh', '猫猫 偷笑 开心 哈哈 接梗'),
    ('cute cat hug', '猫猫 抱抱 安慰 辛苦 温柔'),
    ('cute cat good night', '猫猫 晚安 困了 睡觉 告别'),
    ('cute cat shy', '猫猫 害羞 脸红 谢谢 被夸'),
    ('cat thumbs up', '猫猫 好的 收到 赞 鼓励'),
    ('cute cat love', '猫猫 喜欢 比心 想你 甜蜜'),
    ('cat shocked', '猫猫 惊讶 什么 问号 接梗'),
    ('cute cat hello', '猫猫 你好 早安 打招呼'),
]


def collect(directory):
    from backend.app.services.dm_media import validate_image, LIMIT
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    session = requests.Session()
    for query, label in QUERIES:
        page = 'https://tenor.com/search/' + quote(query.replace(' ', '-')) + '-gifs'
        response = session.get(page, timeout=20, allow_redirects=False)
        response.raise_for_status()
        urls = list(dict.fromkeys(re.findall(r'https://media\d*\.tenor\.com/[^"<>\\\s]+\.gif', response.text)))
        urls.sort(key=lambda url: ('/m/' in urlsplit(url).path, 'AAAAM/' not in url))
        accepted = 0
        for url in urls[:8]:
            try:
                with session.get(url, timeout=20, stream=True, allow_redirects=False) as image:
                    if image.status_code != 200:
                        continue
                    chunks, size = [], 0
                    for chunk in image.iter_content(65536):
                        size += len(chunk)
                        if size > LIMIT:
                            raise ValueError('oversized image')
                        chunks.append(chunk)
                blob = b''.join(chunks)
                ext, _ = validate_image(blob)
                name = hashlib.sha256(blob).hexdigest() + '.' + ext
                (directory / name).write_bytes(blob)
                if not any(entry['file'] == name for entry in entries):
                    entries.append(dict(file=name, label=label, source_url=url, source_page=page, query=query))
                    accepted += 1
                if accepted == 2:
                    break
            except (ValueError, requests.RequestException):
                continue
        print(query, accepted, flush=True)
    (directory / 'manifest.json').write_text(json.dumps(entries, ensure_ascii=False, indent=2))


def import_reviewed(directory, account_ids):
    from backend.app.core.database import SessionLocal
    from backend.app.models import PlatformAccount
    from backend.app.models.direct_message import DmMedia
    from backend.app.services import dm_media
    entries = json.loads((directory / 'reviewed.json').read_text())
    with SessionLocal() as db:
        for account_id in account_ids:
            account = db.get(PlatformAccount, account_id)
            if not account or account.platform != 'xhs':
                raise ValueError('unknown XHS account')
            for entry in entries:
                name = entry['file']
                if Path(name).name != name:
                    raise ValueError('invalid basename')
                blob = (directory / name).read_bytes()
                dm_media.validate_image(blob)
                key = hashlib.sha256((str(account_id) + ':sticker:').encode() + blob).hexdigest()
                asset = db.get(DmMedia, key)
                if not asset:
                    asset = DmMedia(id=key, account_id=account_id, kind='sticker', label=entry['label'][:120],
                        content_type=16, library=True, url='', native={})
                    db.add(asset)
                else:
                    asset.library = True
                dm_media.store_blob(db, asset, blob)
            print('Imported', len(entries), 'reviewed reactions into account', account_id)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, default=Path('.local/reaction-pack'))
    parser.add_argument('--collect', action='store_true')
    parser.add_argument('--import-reviewed', action='store_true')
    parser.add_argument('--account-id', type=int, action='append', default=[])
    args = parser.parse_args()
    if args.collect:
        collect(args.directory)
    if args.import_reviewed:
        if not args.account_id:
            parser.error('--import-reviewed requires --account-id')
        import_reviewed(args.directory, args.account_id)
