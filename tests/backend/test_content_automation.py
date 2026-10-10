"""No real account, network or social writes are used by these regressions."""
from datetime import datetime, timedelta
import json
import random
from types import SimpleNamespace

import pytest

from backend.app.services import content_automation as mod


def draft():
    return {'decision': 'publish', 'title': '包里别塞一天', 'body': '只在附近散步，包里却放了一天的备用品。可以先把通勤杂物拿出来，按这趟路线准备，不必为了小包硬省水。',
            'photos': [{'id': p, 'page_url': f'https://www.pexels.com/photo/test-scene-{p}/',
                        'caption': '真实场景参考'} for p in ('7138819', '5596244')],
            'sources': [{'url': 'https://www.pexels.com/license/', 'observation': '许可'}]}


def notification(own_note=True, peer='reader', target_owner='xiaotao'):
    return {'type': 'comment/note' if own_note else 'comment/comment', 'time': 1791646000000,
            'user_info': {'userid': peer},
            'item_info': {'id': 'note-a', 'user_info': {'userid': 'xiaotao' if own_note else 'other'}},
            'comment_info': {'id': 'comment-a', 'content': '是哪个公园？',
                             'target_comment': {'id': 'our-comment', 'content': '这里好安静',
                                                'user_info': {'userid': target_owner}}}}


def review(ids):
    return {'decision': 'approve', 'text_pass': True, 'viewed_photo_ids': ids,
            'photo_checks': [{'id': i, 'pass': True, 'observation': '已看到木质长椅与周围树木'} for i in ids]}


def test_daily_random_slots_are_durable_and_distinct(tmp_path, monkeypatch):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    instant = datetime(2026, 10, 10, 23, 10, tzinfo=mod.TZ)
    monkeypatch.setattr(mod, 'now', lambda: instant)
    calls = []
    triggers = []
    def request(method, path, data=None):
        calls.append((method, path, data))
        if method == 'POST':
            trigger = {**data, 'id': str(len(triggers) + 1), 'enabled': True}
            triggers.append(trigger)
            return trigger
        return {'triggers': triggers, 'autopilot': {'description': 'mode=post'}}
    config = {'autopilot_id': 'automation', 'pc_account_id': 9}
    executor = mod.Executor(config, ledger, SimpleNamespace(request=request), object())
    executor.schedule()
    first = ledger.state('schedule')
    executor.schedule()
    assert ledger.state('schedule') == first
    assert len([c for c in calls if c[0] == 'POST']) == 2 * mod.SCHEDULE_DAYS
    assert len(ledger.state('schedule_window')['days']) == mod.SCHEDULE_DAYS
    # A new executor must reconcile persisted plans without creating duplicates.
    ledger.db.close()
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    instant += timedelta(minutes=11)
    mod.Executor(config, ledger, SimpleNamespace(request=request), object()).schedule()
    assert ledger.state('schedule') == first
    assert len([c for c in calls if c[0] == 'POST']) == 2 * mod.SCHEDULE_DAYS
    stamps = [mod.parse_time(s) for s in first['slots']]
    assert all(s.date().isoformat() == '2026-10-11' for s in stamps)
    assert 9 <= stamps[0].hour < 12 and 18 <= stamps[1].hour <= 21
    assert stamps[1] - stamps[0] >= timedelta(hours=6)
    assert mod.choose_slots('2026-10-11', random.Random(1)) != mod.choose_slots('2026-10-11', random.Random(2))


def test_missing_schedule_receipt_is_uncertain_and_not_recreated(tmp_path, monkeypatch):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    monkeypatch.setattr(mod, 'now', lambda: datetime(2026, 10, 10, 23, 10, tzinfo=mod.TZ))
    calls = []
    def request(method, path, data=None):
        calls.append((method, path, data))
        return {'triggers': [], 'autopilot': {'description': 'mode=post'}}
    executor = mod.Executor({'autopilot_id': 'automation', 'pc_account_id': 9},
                            ledger, SimpleNamespace(request=request), object())
    notices = []
    monkeypatch.setattr(executor, 'notify', lambda *args: notices.append(args))
    with pytest.raises(mod.RemoteError) as caught:
        executor.schedule()
    assert caught.value.ambiguous
    key = 'schedule-trigger:小桃发布 2026-10-11 · 1'
    assert ledger.state(key)['stage'] == 'uncertain'
    # Other slots may be attempted, but the unacknowledged slot is held.
    with pytest.raises(mod.RemoteError):
        executor.schedule()
    assert notices and notices[0][0] == key
    assert ledger.state(key)['stage'] == 'uncertain'
    assert sum(method == 'POST' and data['label'] == '小桃发布 2026-10-11 · 1'
               for method, _, data in calls) == 1


def test_platform_empty_paragraph_normalization_keeps_content():
    assert mod.platform_text('第一段\n\n第二段\n') == mod.platform_text('第一段\n第二段')
    assert mod.platform_text('第一段\n第二段') != mod.platform_text('第一段\n篡改内容')


def test_model_photo_urls_cannot_be_ssrf_or_local_paths():
    for url in ('http://127.0.0.1/private', 'https://www.pexels.com.evil/photo/x-7138819/',
                '/etc/passwd', 'https://www.pexels.com/photo/x-1234/',
                'https://www.pexels.com/photo/x-7138819/?redirect=http://localhost'):
        value = draft(); value['photos'][0]['page_url'] = url
        with pytest.raises(ValueError):
            mod.validate_draft(value)
    value = mod.validate_draft(draft())
    assert value['body'] == draft()['body']
    assert value['photos'] == draft()['photos'] and value['sources'] == draft()['sources']
    value = draft(); value['photos'][1] = value['photos'][0]
    with pytest.raises(ValueError):
        mod.validate_draft(value)


def test_review_requires_every_actual_photo_and_explicit_checks():
    ids = ['7138819', '5596244']
    assert mod.validate_review(review(ids), ids)
    assert not mod.validate_review(review(ids[:1]), ids)
    value = review(ids); value['photo_checks'][0]['pass'] = False
    assert not mod.validate_review(value, ids)
    value = review(ids); value['text_pass'] = 'true'
    assert not mod.validate_review(value, ids)


def test_two_comment_directions_and_self_loop_prevention():
    own = mod.normalize_mention(notification(), 'xiaotao', {'xiaotao', 'leo'})
    outside = mod.normalize_mention(notification(False), 'xiaotao', {'xiaotao', 'leo'})
    assert own['kind'] == 'own_note_comment'
    assert outside['kind'] == 'reply_to_our_comment'
    assert outside['comment_id'] == 'comment-a'
    assert mod.normalize_mention(notification(False, target_owner='stranger'), 'xiaotao', set()) is None
    assert mod.normalize_mention(notification(peer='leo'), 'xiaotao', {'xiaotao', 'leo'}) is None
    assert mod.normalize_mention(notification(peer='xiaotao'), 'xiaotao', {'xiaotao'}) is None
    raw = notification(); raw['type'] = 'likes/note'
    assert mod.normalize_mention(raw, 'xiaotao', set()) is None


def test_post_quota_counts_unknown_receipts_and_prevents_reclaim(tmp_path):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    for key in ('a', 'b', 'c'):
        ledger.add(key, 'post', {})
        ledger.update(key, stage='ready')
    assert ledger.reserve_write('a')
    assert not ledger.reserve_write('a')
    ledger.update('a', stage='uncertain')
    assert ledger.reserve_write('b')
    ledger.update('b', stage='published')
    assert not ledger.reserve_write('c')


def test_receipt_verification_on_next_day_does_not_move_daily_quota(tmp_path, monkeypatch):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    instant = datetime(2026, 10, 10, 20, tzinfo=mod.TZ)
    monkeypatch.setattr(mod, 'now', lambda: instant)
    ledger.add('old', 'post', {}); ledger.update('old', stage='ready')
    assert ledger.reserve_write('old'); ledger.update('old', stage='published')
    instant += timedelta(days=1)
    ledger.update('old', payload={'verification': 'done'})
    for key in ('today-a', 'today-b'):
        ledger.add(key, 'post', {}); ledger.update(key, stage='ready')
        assert ledger.reserve_write(key)


def test_frozen_channel_submission_reuses_id_and_owned_account(tmp_path):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3'); ledger.add('test', 'comment', {})
    calls = []
    def send(*args, **kwargs):
        calls.append((args, kwargs)); return {'id': 'event-a'}
    channel = SimpleNamespace(send=send)
    executor = mod.Executor({'pc_account_id': 9, 'route_id': 'route'}, ledger, object(), channel)
    executor.submit('test', 'original')
    executor.submit('test', 'changed')
    assert calls[0] == calls[1]
    assert calls[0][0] == (9,)
    assert calls[0][1]['text'] == 'original'


def test_interrupted_platform_writes_never_repeat(tmp_path):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    executor = mod.Executor({'pc_account_id': 9}, ledger, object(), object())
    executor.notify = lambda *args: None
    ledger.add('post', 'post', {}, 'issue'); ledger.update('post', stage='publishing')
    executor.process_post(ledger.entry('post'))
    assert ledger.entry('post')['stage'] == 'uncertain'
    ledger.add('comment', 'comment', {'peer_id': 'reader'}); ledger.update('comment', stage='sending')
    executor.process_comment(ledger.entry('comment'))
    assert ledger.entry('comment')['stage'] == 'uncertain'


def test_comment_reply_target_is_bound_to_notification(tmp_path, monkeypatch):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    payload = mod.normalize_mention(notification(False), 'xiaotao', {'xiaotao'})
    payload['reply'] = {'text': '看天气和路线带就好～'}
    ledger.add('reply', 'comment', payload); ledger.update('reply', stage='ready')
    captured = []
    executor = mod.Executor({'pc_account_id': 9}, ledger, object(), object())
    executor.pc = lambda: SimpleNamespace(post_comment=lambda *args: captured.append(args) or {
        'success': True, 'data': {'comment': {'id': 'sent-comment'}}})
    executor.verify_identity = lambda: None
    executor.assert_enabled = lambda: None
    monkeypatch.setattr(mod.xhs_network, 'check', lambda: None)
    monkeypatch.setattr(mod.xhs_network, 'require_ready', lambda: None)
    executor.process_comment(ledger.entry('reply'))
    assert captured == [('note-a', '看天气和路线带就好～', 'comment-a')]
    assert ledger.entry('reply')['stage'] == 'replied'


def test_comment_lost_ack_is_not_retried(tmp_path, monkeypatch):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    payload = mod.normalize_mention(notification(False), 'xiaotao', {'xiaotao'})
    payload['reply'] = {'text': '按路线准备就好'}
    ledger.add('reply', 'comment', payload); ledger.update('reply', stage='ready')
    calls = []
    def post(*args):
        calls.append(args); raise TimeoutError('synthetic lost ACK')
    executor = mod.Executor({'pc_account_id': 9}, ledger, object(), object())
    executor.pc = lambda: SimpleNamespace(post_comment=post)
    executor.verify_identity = lambda: None; executor.assert_enabled = lambda: None
    executor.notify = lambda *args: None
    monkeypatch.setattr(mod.xhs_network, 'check', lambda: None)
    monkeypatch.setattr(mod.xhs_network, 'require_ready', lambda: None)
    executor.process_comment(ledger.entry('reply'))
    executor.process_comment(ledger.entry('reply'))
    assert ledger.entry('reply')['stage'] == 'uncertain'
    assert len(calls) == 1


def test_identity_read_failure_waits_but_wrong_identity_stops(tmp_path):
    executor = mod.Executor({'expected_external_user_id': 'xiaotao'}, mod.Ledger(tmp_path / 'state.sqlite3'), object(), object())
    def unavailable():
        raise RuntimeError('synthetic cold-start dependency timeout')
    executor.pc = lambda: SimpleNamespace(get_self_info=unavailable)
    with pytest.raises(mod.Paused):
        executor.verify_identity()
    executor.pc = lambda: SimpleNamespace(get_self_info=lambda: {'user_id': 'other-account'})
    with pytest.raises(ValueError):
        executor.verify_identity()
    executor.pc = lambda: SimpleNamespace(get_self_info=lambda: {'user_id': 'xiaotao'})
    executor.verify_identity()


def test_publish_lost_ack_persists_intent_job_and_never_reposts(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.app.core.database import Base
    from backend.app.adapters.xhs.creator_login_adapter import XhsCreatorLoginAdapter
    engine = create_engine('sqlite:///' + str(tmp_path / 'business.sqlite3'))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(mod, 'SessionLocal', sessions)
    monkeypatch.setattr(mod.xhs_network, 'check', lambda: None)
    monkeypatch.setattr(mod.xhs_network, 'require_ready', lambda: None)
    monkeypatch.setattr(XhsCreatorLoginAdapter, 'get_user_info', lambda *args: {'external_user_id': 'xiaotao'})
    value = mod.validate_draft(draft()); ids = [p['id'] for p in value['photos']]
    images = {}
    for i, pid in enumerate(ids):
        path = tmp_path / (pid + '.jpg'); path.write_bytes(b'synthetic-image-' + bytes([i])); images[pid] = path
    hashes = {pid: mod.hashlib.sha256(path.read_bytes()).hexdigest() for pid, path in images.items()}
    payload = {'draft': value, 'review': review(ids), 'photo_hashes': hashes,
               'review_input_fingerprint': mod.hashlib.sha256(mod.dump([value, hashes]).encode()).hexdigest()}
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    ledger.add('post', 'post', payload, 'synthetic-issue'); ledger.update('post', stage='ready')
    calls = []
    def post(info):
        calls.append(info)
        assert ledger.entry('post')['stage'] == 'publishing'
        raise TimeoutError('synthetic lost ACK')
    creator = SimpleNamespace(upload_media=lambda *args: {'fileIds': 'uploaded', 'width': 800, 'height': 1000}, post_note=post)
    executor = mod.Executor({'user_id': 1, 'creator_account_id': 3, 'expected_external_user_id': 'xiaotao'},
                            ledger, object(), object())
    executor.photo = lambda p: images[p['id']]
    executor.creator = lambda: creator; executor.account = lambda *args: 'synthetic=1'
    executor.verify_identity = lambda: None; executor.assert_enabled = lambda: None
    executor.notify = lambda *args: None
    def creator_unavailable(*args):
        raise RuntimeError('synthetic identity read timeout')
    monkeypatch.setattr(XhsCreatorLoginAdapter, 'get_user_info', creator_unavailable)
    with pytest.raises(mod.Paused):
        executor.publish(ledger.entry('post'))
    assert ledger.entry('post')['stage'] == 'ready' and not ledger.entry('post')['job_id']
    assert calls == []
    monkeypatch.setattr(XhsCreatorLoginAdapter, 'get_user_info', lambda *args: {'external_user_id': 'xiaotao'})
    executor.publish(ledger.entry('post'))
    assert ledger.entry('post')['stage'] == 'uncertain'
    assert ledger.entry('post')['job_id']
    executor.process_post(ledger.entry('post'))
    assert len(calls) == 1
    with sessions() as db:
        job = db.get(mod.PublishJob, ledger.entry('post')['job_id'])
        assert job.platform_account_id == 3 and job.status == 'uncertain'
    start = mod.parse_time(json.loads(ledger.entry('post')['payload'])['write_started_at']).timestamp()
    note_id = 'a' * 24
    card = {'note_id': note_id, 'title': value['title'], 'desc': value['body'].replace('\n\n', '\n'),
            'user': {'user_id': 'impostor'}, 'image_list': [{}, {}], 'time': int((start + 1) * 1000)}
    executor.pc = lambda: SimpleNamespace(
        get_recent_user_notes=lambda *args: {'notes': [{'note_id': note_id, 'display_title': value['title']}]},
        get_note_info=lambda *args: (True, 'ok', {'data': {'items': [{'id': note_id, 'note_card': card}]}}))
    executor.refresh_context = lambda: None
    executor.verify_posts()
    assert ledger.entry('post')['stage'] == 'uncertain'
    card['user']['user_id'] = 'xiaotao'; ledger.set_state('post_verify_at', 0)
    executor.verify_posts()
    assert ledger.entry('post')['stage'] == 'published'
    assert json.loads(ledger.entry('post')['payload'])['receipt']['reconciled_by_read'] is True
    assert len(calls) == 1
    engine.dispose()
