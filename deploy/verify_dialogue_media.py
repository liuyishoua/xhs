"""Replay a real synthetic model result through XHS using fake transport only."""
import argparse
from datetime import timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.core.database import Base
from backend.app.core.time import shanghai_now, SHANGHAI_TZ
from backend.app.models import User, PlatformAccount
from backend.app.models.direct_message import DirectMessage, DmMedia, DmListener
from backend.app.services import dm_media, dm_service as dm


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--report', required=True)
    p.add_argument('--case', default='sticker_turn')
    args = p.parse_args()
    records = json.loads(Path(args.report).read_text())
    record = next(x for x in records if x['case'] == args.case)
    proposals = record['output'].get('reply_messages', [])
    assert any(m.get('type') == 'sticker' for m in proposals), 'Need a native sticker result'
    # Read cached fixture bytes only; no platform credentials or requests.
    with sqlite3.connect(ROOT / 'data/spider_xhs.db') as source:
        originals = dict(source.execute("SELECT label,filename FROM xhs_dm_media WHERE account_id=1 AND library=1 AND filename!=''"))
    with tempfile.TemporaryDirectory(prefix='fake-dm-model-replay-') as directory:
        home = Path(directory)
        engine = create_engine('sqlite:///' + str(home / 'db'))
        Base.metadata.create_all(engine)
        original_root = dm_media.ROOT
        dm_media.ROOT = home
        try:
            with sessionmaker(bind=engine, expire_on_commit=False)() as db:
                db.add(User(id=1, username='synthetic-owner', password_hash='synthetic'))
                db.add(PlatformAccount(id=1, user_id=1, platform='xhs', sub_type='pc', external_user_id='synthetic-own'))
                db.add(DmListener(account_id=1, enabled=True, auto_reply=True, control_mode='full_access'))
                for candidate in record['catalog']:
                    original = originals[candidate['label']]
                    filename = candidate['asset_id'] + '.gif'
                    shutil.copyfile(ROOT / '.local/dm-media' / original, home / filename)
                    db.add(DmMedia(id=candidate['asset_id'], account_id=1, label=candidate['label'], kind='sticker',
                                   filename=filename, content_type=16, mime='image/gif', library=True))
                now = shanghai_now()
                row = DirectMessage(account_id=1, message_id='synthetic-replay', peer_id='synthetic-peer', text='来斗图',
                    state='queued', channel_payload={'account_id': 'synthetic-replay-account', 'message_id': 'synthetic-replay',
                    'context': {'control_mode': 'full_access', 'media_prepared': True, 'media_catalog': record['catalog'],
                                'history': {'complete': True, 'since': (now-timedelta(hours=48)).isoformat(), 'until': now.isoformat()}}})
                db.add(row); db.commit()
                class FakeChannel:
                    def send(self, **kwargs):
                        return {'id': 'synthetic-model-result'}
                    def status(self, *args):
                        return {'state': 'succeeded', 'result': {'output': {'structured': record['output']}}}
                dm.poll_channel(db, row, FakeChannel())
                sent = []
                def fake_upload(path):
                    with Image.open(path) as image:
                        assert image.format == 'GIF'
                        width, height = image.size
                    return {'url': 'https://msg-img.xhscdn.com/synthetic-replay', 'file_id': 'synthetic-gif-id',
                            'width': width, 'height': height}
                while row.state == 'ready':
                    if 'reply_due_at' in row.receipt:
                        row.receipt = {**row.receipt, 'reply_due_at': (shanghai_now()-timedelta(seconds=1)).isoformat()}
                        db.commit()
                    dm_media.prepare_outgoing(db, row, fake_upload)
                    claimed = dm.claim_send(db, row.id)
                    assert claimed is not None
                    bubble = claimed.receipt['reply_message']
                    if bubble['type'] == 'sticker':
                        native = json.loads(bubble['content'])
                        assert bubble['content_type'] == 16 and native['isGif'] == 1
                    sent.append({'type': bubble['type'], 'content_type': bubble['content_type']})
                    dm.finish_send(db, row.id, {'mid': claimed.outbound_mid, 'code': 0, 'msg': 'ok',
                        'messageid': 'synthetic-ack-' + str(len(sent)),
                        'ts': int(shanghai_now().replace(tzinfo=SHANGHAI_TZ).timestamp()*1000)})
                assert row.state == 'sent' and len(sent) == len(proposals)
                assert dm.claim_send(db, row.id) is None
                print(json.dumps({'case': args.case, 'model_execution': record['execution'],
                                  'fake_transport_only': True, 'native_messages': sent,
                                  'matched_ack': True, 'duplicate_send_blocked': True}, ensure_ascii=False))
        finally:
            dm_media.ROOT = original_root
            engine.dispose()


if __name__ == '__main__':
    main()
