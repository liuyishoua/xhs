"""Account-scoped reaction proposals and acknowledged-message provenance."""
import io
from datetime import timedelta
import json
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import pytest
from backend.app.core.database import Base
from backend.app.core.time import shanghai_now, SHANGHAI_TZ
from backend.app.models import User, PlatformAccount
from backend.app.models.direct_message import DirectMessage, DmListener, DmMedia
from backend.app.services import dm_service as dm, dm_media, dm_history, dm_dialogue


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'db'))
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dm_media, 'ROOT', tmp_path)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        session.add(User(id=1, username='owner', password_hash='synthetic'))
        session.add_all([PlatformAccount(id=1, user_id=1, platform='xhs', sub_type='pc', external_user_id='own'),
                         PlatformAccount(id=2, user_id=1, platform='xhs', sub_type='pc', external_user_id='peer')])
        session.add(DmListener(account_id=1, enabled=True, auto_reply=True, control_mode='full_access'))
        session.commit()
        yield session
    engine.dispose()


def row(db, text='哈哈这个真好笑'):
    item = DirectMessage(account_id=1, message_id='trigger', peer_id='peer', text=text, media=[],
                         channel_payload={'account_id': 'synthetic-account'}, state='history_pending')
    db.add(item); db.commit()
    dm.prepare_history(db, item, lambda *a, **kw: {'result': {'success': True}, 'out_message_list': []})
    return item


def asset(db, tmp_path, key, label, account=1, exists=True):
    if exists:
        frames = [Image.new('RGB', (8, 8), color) for color in ('red', 'blue')]
        frames[0].save(tmp_path / (key + '.gif'), save_all=True, append_images=frames[1:], duration=100, loop=0)
    item = DmMedia(id=key, account_id=account, label=label, kind='sticker', content_type=16,
                   filename=key + '.gif', mime='image/gif', library=True)
    db.add(item); db.commit()
    return item


def test_text_trigger_offers_real_account_reaction_and_native_send(db, tmp_path):
    asset(db, tmp_path, 'laugh', '偷笑 哈哈 开心')
    asset(db, tmp_path, 'foreign', '偷笑 哈哈', account=2)
    asset(db, tmp_path, 'missing', '偷笑 哈哈', exists=False)
    item = row(db)
    uploads = []
    class Client:
        def upload(self, account, blob, filename):
            with Image.open(io.BytesIO(blob)) as image:
                assert image.format == 'JPEG'
            uploads.append((account, filename))
            return {'id': 'synthetic-attachment'}
        def send(self, **kwargs):
            assert kwargs['context']['media_catalog'][0]['source'] == 'reaction_library'
            assert kwargs['images'] == ['synthetic-attachment']
            return {'id': 'synthetic-event'}
        def status(self, *args):
            return {'state': 'succeeded', 'result': {'output': {'structured': {'decision': 'reply', 'requires_human': False,
                    'reply_messages': [{'type': 'sticker', 'asset_id': 'laugh', 'delay_seconds': 0}]}}}}
    dm.poll_channel(db, item, Client())
    assert item.state == 'ready' and uploads == [('synthetic-account', 'laugh.jpg')]
    platform_uploads = []
    def upload(path):
        platform_uploads.append(path)
        return {'url': 'https://msg-img.xhscdn.com/synthetic', 'file_id': 'gif-id', 'width': 8, 'height': 8}
    dm_media.prepare_outgoing(db, item, upload)
    sending = dm.claim_send(db, item.id)
    assert sending is not None
    bubble = sending.receipt['reply_message']
    native = json.loads(bubble['content'])
    assert bubble['content_type'] == 16 and native['isGif'] == 1 and native['smileId'] == 'gif-id'
    assert platform_uploads == [tmp_path / 'laugh.gif']
    dm.finish_send(db, item.id, {'mid': sending.outbound_mid, 'messageid': 'sent-gif', 'code': 0, 'msg': 'ok',
                                'ts': int(shanghai_now().replace(tzinfo=SHANGHAI_TZ).timestamp() * 1000)})
    assert item.state == 'sent'
    assert dm.claim_send(db, item.id) is None


def test_current_visual_priority_account_scope_and_recent_reaction_exclusion(db, tmp_path):
    asset(db, tmp_path, 'current', '图片')
    asset(db, tmp_path, 'laugh', '偷笑 哈哈')
    asset(db, tmp_path, 'used', '偷笑 开心')
    asset(db, tmp_path, 'hug', '抱抱 安慰')
    asset(db, tmp_path, 'foreign', '偷笑', account=2)
    item = row(db)
    item.media = [{'asset_id': 'current'}]
    snapshot = {'messages': [{'direction': 'outbound', 'text': '', 'media': [{'asset_id': 'used'}]}]}
    selected = dm_media.select_reply_assets(db, item, snapshot)
    assert selected[0] == 'current' and 'foreign' not in selected
    # Recently sent media remains readable as conversation context but is not
    # offered again as a new reaction-library candidate.
    assert selected.count('used') == 1
    assert selected.index('laugh') < selected.index('hug')


def test_optional_reaction_upload_failure_keeps_text_available(db, tmp_path):
    asset(db, tmp_path, 'laugh', '偷笑 哈哈')
    item = row(db)
    class Client:
        def upload(self, *args):
            raise RuntimeError('secret local path or request detail')
    dm_media.prepare_inputs(db, item, Client())
    context = item.channel_payload['context']
    assert context['media_prepared'] is True
    assert not item.channel_payload.get('images')
    assert context['media_catalog'][0].get('image_number') is None
    assert 'secret' not in json.dumps(context)
    with pytest.raises(ValueError, match='只能发送'):
        dm_media.reply_message(db, item, {'reply_message': {'type': 'sticker', 'asset_id': 'laugh'}})
    # The incoming picture is mandatory input; never silently pretend to see it.
    item.media = [{'asset_id': 'laugh'}]
    with pytest.raises(RuntimeError):
        dm_media.prepare_inputs(db, item, Client())


def test_provenance_marks_both_managed_sides_without_claiming_unconfirmed_manual(db):
    item = row(db)
    now = shanghai_now()
    since = (now - timedelta(hours=48)).isoformat()
    def acknowledged(account, peer, text, mid, stamp):
        db.add(DirectMessage(account_id=account, peer_id=peer, message_id='job-' + mid, text='incoming',
              channel_payload={}, state='sent', reply_text=text,
              receipt={'reply_messages': [{'status': 'sent', 'text': text,
                      'ack': {'code': 0, 'msg': 'ok', 'messageid': mid, 'ts': stamp}}]}))
    ts = int(now.replace(tzinfo=SHANGHAI_TZ).timestamp() * 1000)
    acknowledged(1, 'peer', '哈哈～', 'self-generated', ts)
    acknowledged(2, 'own', '好呀～', 'peer-generated', ts)
    # Same owner, same text, different conversation must not contaminate style.
    acknowledged(1, 'someone-else', '人工未确认', 'elsewhere', ts)
    local = dm_history.save_message(db, 1, 'peer', 'self-generated', 'outbound', '哈哈～', now)
    db.commit()
    snapshot = {'history': {'since': since}, 'messages': [
        {'message_id': 'xhs-history:' + str(local.id), 'direction': 'outbound', 'text': '哈哈～', 'sent_at': now.isoformat()},
        # Platform can alias message IDs across live/history representations.
        {'message_id': 'peer-alias', 'direction': 'inbound', 'text': '好呀～', 'sent_at': now.isoformat()},
        {'message_id': 'unconfirmed', 'direction': 'outbound', 'text': '人工未确认', 'sent_at': now.isoformat()}]}
    result = dm_dialogue.context(db, item, snapshot)
    assert result['peer_is_managed_account'] is True
    assert result['known_automated_message_ids'] == ['xhs-history:' + str(local.id), 'peer-alias']
    assert result['self_style_candidates'] == [{'message_id': 'unconfirmed', 'text': '人工未确认', 'provenance': 'unconfirmed'}]
    snapshot['messages'][1]['sent_at'] = (now - timedelta(minutes=1)).isoformat()
    assert 'peer-alias' not in dm_dialogue.context(db, item, snapshot)['known_automated_message_ids']


def test_failed_or_unconfirmed_ack_is_not_style_provenance(db):
    item = row(db); now = shanghai_now()
    db.add(DirectMessage(account_id=1, peer_id='peer', message_id='failed', text='incoming', channel_payload={},
                         state='failed', reply_text='未成功', receipt={'code': 0, 'messageid': 'outbound-id'}))
    db.commit()
    snapshot = {'history': {'since': (now-timedelta(hours=48)).isoformat()}, 'messages': [
        {'message_id': 'outbound-id', 'direction': 'outbound', 'text': '未成功', 'sent_at': now.isoformat()}]}
    assert dm_dialogue.context(db, item, snapshot)['known_automated_message_ids'] == []
