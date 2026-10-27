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


def test_comment_preflight_retries_same_event_with_backoff_and_limit(tmp_path, monkeypatch):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    ledger.add('comment:2:test', 'comment', {'text': 'cc'})
    ledger.update('comment:2:test', stage='thinking', event_id='original-event')
    retries = []
    channel = SimpleNamespace(status=lambda *args: {'state': 'failed_preflight', 'error': 'Multica 返回 HTTP 409'},
                              retry=lambda *args: retries.append(args))
    executor = mod.Executor({'pc_account_id': 2}, ledger, object(), channel)
    instant = [1000.0]
    monkeypatch.setattr(mod.time, 'time', lambda: instant[0])
    for delay in (30, 120, 300):
        assert executor.channel_result(ledger.entry('comment:2:test')) is None
        assert executor.channel_result(ledger.entry('comment:2:test')) is None
        instant[0] += delay
        assert executor.channel_result(ledger.entry('comment:2:test')) is None
    assert retries == [('original-event', 2)] * 3
    with pytest.raises(ValueError, match='HTTP 409'):
        executor.channel_result(ledger.entry('comment:2:test'))


@pytest.mark.parametrize('state', ['uncertain', 'failed', 'cancelled'])
def test_comment_ambiguous_or_delivered_model_turn_never_retried(tmp_path, state):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    ledger.add('comment:2:test', 'comment', {})
    ledger.update('comment:2:test', stage='thinking', event_id='original-event')
    channel = SimpleNamespace(status=lambda *args: {'state': state}, retry=lambda *args: pytest.fail('unsafe retry'))
    executor = mod.Executor({'pc_account_id': 2}, ledger, object(), channel)
    with pytest.raises(ValueError):
        executor.channel_result(ledger.entry('comment:2:test'))


class ScheduleAPI:
    def __init__(self):
        self.triggers = []; self.posts = 0; self.description = 'mode=post'; self.lose_ack = False

    def request(self, method, path, data=None):
        if method == 'GET':
            return {'triggers': json.loads(json.dumps(self.triggers)), 'autopilot': {'description': self.description}}
        if method == 'POST':
            self.posts += 1
            trigger = {**data, 'id': str(self.posts), 'enabled': True}
            self.triggers.append(trigger)
            if self.lose_ack:
                self.lose_ack = False
                raise mod.RemoteError(0, True)
            return trigger
        if '/triggers/' in path:
            item = next(t for t in self.triggers if t['id'] == path.rsplit('/', 1)[-1])
            if method == 'DELETE': self.triggers.remove(item)
            else: item.update(data)
        else: self.description = data['description']
        return {}


def test_two_daily_triggers_preserve_times_on_restart_and_reroll_next_day(tmp_path, monkeypatch):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    instant = datetime(2026, 12, 31, 0, 10, tzinfo=mod.TZ)
    monkeypatch.setattr(mod, 'now', lambda: instant)
    api = ScheduleAPI(); config = {'autopilot_id': 'automation', 'pc_account_id': 9}
    executor = mod.Executor(config, ledger, api, object())
    executor.schedule(); first = ledger.state('schedule')
    ids = [t['id'] for t in api.triggers]
    mod.Executor(config, ledger, api, object()).schedule()
    assert ledger.state('schedule') == first and api.posts == 2
    assert len(api.triggers) == 2
    assert all(t['cron_expression'].split()[2:] == ['*', '*', '*'] for t in api.triggers)
    assert len(ledger.state('schedule_window')['days']) == 1
    assert 'planned_slots' not in api.description
    stamps = [mod.parse_time(s) for s in first['slots']]
    assert all(s.date().isoformat() == first['day'] for s in stamps)
    assert 9 <= stamps[0].hour < 12 and 18 <= stamps[1].hour <= 21
    assert stamps[1] - stamps[0] >= timedelta(hours=6)
    assert first['day'] == '2026-12-31'
    instant += timedelta(days=1)
    executor.schedule()
    assert ledger.state('schedule')['day'] == '2027-01-01'
    assert api.posts == 2 and [t['id'] for t in api.triggers] == ids
    assert mod.choose_slots('2026-10-11', random.Random(1)) != mod.choose_slots('2026-10-11', random.Random(2))


def test_schedule_reconciles_lost_ack_and_missing_trigger_without_duplicates(tmp_path, monkeypatch):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3'); api = ScheduleAPI(); api.lose_ack = True
    instant = datetime(2026, 10, 11, 0, tzinfo=mod.TZ)
    monkeypatch.setattr(mod, 'now', lambda: instant)
    executor = mod.Executor({'autopilot_id': 'a', 'pc_account_id': 9}, ledger, api, object())
    executor.notify = lambda *args: None
    with pytest.raises(mod.RemoteError): executor.schedule()
    executor.schedule()
    assert api.posts == 2 and len(api.triggers) == 2
    first = ledger.state('schedule')
    api.triggers.pop(); api.triggers.append({'id': 'manual', 'label': 'manual', 'kind': 'schedule'})
    instant += timedelta(minutes=11); executor.schedule()
    assert api.posts == 3 and len(api.triggers) == 3
    assert ledger.state('schedule') == first
    assert any(t['id'] == 'manual' for t in api.triggers)


def test_daily_schedule_only_changes_its_own_two_triggers(tmp_path, monkeypatch):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3'); api = ScheduleAPI()
    instant = datetime(2026, 10, 11, 12, tzinfo=mod.TZ)
    monkeypatch.setattr(mod, 'now', lambda: instant)
    api.triggers.append({'id': 'manual', 'label': 'manual', 'kind': 'schedule'})
    mod.Executor({'autopilot_id': 'a'}, ledger, api, object()).schedule()
    assert api.posts == 2 and len(api.triggers) == 3
    assert api.triggers[0] == {'id': 'manual', 'label': 'manual', 'kind': 'schedule'}
    assert len(ledger.state('schedule_window')['days']) == 1


def test_restart_after_a_daily_trigger_fired_does_not_schedule_it_again(tmp_path, monkeypatch):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3'); api = ScheduleAPI()
    instant = datetime(2026, 10, 11, 9, 2, tzinfo=mod.TZ)
    monkeypatch.setattr(mod, 'now', lambda: instant)
    monkeypatch.setattr(mod, 'choose_slots', lambda day: [f'{day}T10:00:00+08:00', f'{day}T20:00:00+08:00'])
    api.triggers = [{'id': 'morning', 'kind': 'schedule', 'enabled': True, 'label': '上午随机发布',
        'cron_expression': '1 9 * * *', 'timezone': 'Asia/Shanghai',
        'last_fired_at': '2026-10-11T01:01:00Z'}]
    mod.Executor({'autopilot_id': 'a'}, ledger, api, object()).schedule()
    assert api.triggers[0]['cron_expression'] == '1 9 * * *'
    assert ledger.state('schedule')['slots'][0] == '2026-10-11T09:01:00+08:00'
    assert api.posts == 1


def test_human_managed_account_comment_is_ingested_but_self_never_is(tmp_path, monkeypatch):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    executor = mod.Executor({'pc_account_id': 9, 'expected_external_user_id': 'xiaotao', 'started_at': '2026-10-01T00:00:00+08:00'}, ledger, object(), object())
    raw = notification(peer='leo'); raw['comment_info']['content'] = '优秀👍'
    executor.pc = lambda: SimpleNamespace(get_mentions=lambda cursor: {'message_list': [raw, notification(peer='xiaotao')], 'has_more': False})
    executor.poll_comments()
    assert len(ledger.rows('comment')) == 1
    assert json.loads(ledger.rows('comment')[0]['payload'])['text'] == '优秀👍'


def test_two_enabled_accounts_accept_manual_comments_and_ignore_actual_auto_writes(tmp_path):
    base = {'user_id': 1, 'pc_account_id': 2, 'expected_external_user_id': 'xiaotao',
            'started_at': '2026-10-01T00:00:00+08:00'}
    peer = {**base, 'comment_only': True, 'pc_account_id': 1, 'expected_external_user_id': 'leo'}
    peach = mod.Ledger(mod.comment_ledger_path(base, tmp_path))
    leo = mod.Ledger(mod.comment_ledger_path(peer, tmp_path))
    for config, ledger in [(base, peach), (peer, leo)]:
        ledger.set_state(mod.comment_settings_key(config), {'enabled': True, 'auto_reply': True})
    leo.add('sent', 'comment', {})
    leo.update('sent', stage='submitted', external_id='leo-auto-reply')
    peach.set_state('diagnostic_comment_ids', ['diagnostic-reply'])
    guard = mod.automated_comment_guard([base, peer], tmp_path)
    manual = notification(peer='leo'); manual['comment_info']['content'] = '优秀👍'
    automated = notification(peer='leo'); automated['comment_info']['id'] = 'leo-auto-reply'
    diagnostic = notification(peer='leo'); diagnostic['comment_info']['id'] = 'diagnostic-reply'
    executor = mod.Executor({**base, 'automated_comment_guard': guard}, peach, object(), object())
    executor.pc = lambda: SimpleNamespace(get_mentions=lambda cursor: {
        'message_list': [manual, automated, diagnostic, notification(peer='xiaotao')], 'has_more': False})
    executor.poll_comments()
    rows = peach.rows('comment')
    assert len(rows) == 1 and json.loads(rows[0]['payload'])['text'] == '优秀👍'
    # A reply committed during the preceding account tick must be included in
    # the next account's guard, even if its listener has since been disabled.
    peach.update(rows[0]['id'], stage='submitted', external_id='peach-new-auto-reply')
    peach.set_state(mod.comment_settings_key(base), {'enabled': False, 'auto_reply': False})
    assert 'peach-new-auto-reply' in mod.automated_comment_guard([base, peer], tmp_path)['ids']


def test_lost_comment_receipt_is_guarded_by_frozen_intent_with_account_isolation(tmp_path):
    base = {'user_id': 1, 'pc_account_id': 2, 'expected_external_user_id': 'xiaotao'}
    peer = {**base, 'comment_only': True, 'pc_account_id': 1, 'expected_external_user_id': 'leo'}
    foreign = {**peer, 'user_id': 99, 'pc_account_id': 77, 'expected_external_user_id': 'other-owner'}
    ledger = mod.Ledger(mod.comment_ledger_path(peer, tmp_path))
    ledger.add('unknown', 'comment', {'note_id': 'note-a', 'comment_id': 'our-comment', 'reply': {'text': '谢谢'}})
    ledger.update('unknown', stage='uncertain')
    alien = mod.Ledger(mod.comment_ledger_path(foreign, tmp_path))
    alien.add('foreign', 'comment', {})
    alien.update('foreign', stage='submitted', external_id='foreign-comment')
    guard = mod.automated_comment_guard([base, peer, foreign], tmp_path)
    incoming = notification(False, peer='leo'); incoming['comment_info']['content'] = '谢谢'
    assert mod.is_automated_comment(incoming, guard)
    incoming['comment_info']['content'] = '手机上写的新留言'
    assert not mod.is_automated_comment(incoming, guard)
    incoming['comment_info']['content'] = '谢谢'
    incoming['user_info']['userid'] = 'another-human'
    assert not mod.is_automated_comment(incoming, guard)
    assert 'foreign-comment' not in guard['ids']


def test_first_enable_does_not_scan_unrelated_old_notification_history(tmp_path):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    executor = mod.Executor({'pc_account_id': 9, 'expected_external_user_id': 'xiaotao',
                            'started_at': '2026-10-11T00:00:00+08:00'}, ledger, object(), object())
    calls = []
    def page(cursor):
        calls.append(cursor)
        assert len(calls) == 1, 'Do not paginate years of pre-listener notifications'
        return {'message_list': [notification()], 'has_more': True, 'cursor': 'older-page'}
    executor.pc = lambda: SimpleNamespace(get_mentions=page)
    executor.poll_comments()
    assert not ledger.rows('comment') and ledger.state('comment_health')['state'] == 'listening'
    assert calls == ['']


def test_comment_listener_stop_and_generation_only_preserve_queue_without_io(tmp_path, monkeypatch):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    config = {'user_id': 1, 'pc_account_id': 9, 'expected_external_user_id': 'xiaotao', 'route_id': 'route'}
    executor = mod.Executor(config, ledger, object(), object())
    def forbidden(*args): raise AssertionError('unexpected external IO')
    executor.pc = executor.submit = executor.verify_identity = forbidden
    settings = {**mod.comment_settings(config, ledger.state), 'enabled': False}
    ledger.set_state(mod.comment_settings_key(config), settings)
    ledger.add('comment', 'comment', {'peer_id': 'reader'})
    executor.poll_comments(); executor.process_comment(ledger.entry('comment'))
    assert ledger.entry('comment')['stage'] == 'received'
    settings.update(enabled=True, auto_reply=False)
    ledger.set_state(mod.comment_settings_key(config), settings)
    ledger.update('comment', stage='ready', payload={'peer_id': 'reader', 'reply': {'text': '草稿'}})
    executor.process_comment(ledger.entry('comment'))
    assert ledger.entry('comment')['stage'] == 'ready'
    # Scope changes do not inherit another account's stop setting.
    assert mod.comment_settings({**config, 'pc_account_id': 10}, ledger.state)['enabled']


def test_request_approval_sends_only_approved_text_and_stop_during_identity_check_wins(tmp_path, monkeypatch):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    config = {'pc_account_id': 9, 'route_id': 'route'}
    settings = {**mod.comment_settings(config, ledger.state), 'control_mode': 'request_approval'}
    ledger.set_state(mod.comment_settings_key(config), settings)
    payload = {'peer_id': 'reader', 'note_id': 'note', 'comment_id': 'comment', 'reply': {'text': '已生成'},
               'comment_policy': mod.comment_policy(settings)}
    ledger.add('reply', 'comment', payload); ledger.update('reply', stage='ready')
    captured = []
    executor = mod.Executor(config, ledger, object(), object())
    executor.pc = lambda: SimpleNamespace(post_comment=lambda *args: captured.append(args) or {'data': {'comment': {'id': 'sent'}}})
    executor.assert_enabled = executor.verify_identity = lambda: None
    monkeypatch.setattr(mod.xhs_network, 'check', lambda: None)
    monkeypatch.setattr(mod.xhs_network, 'require_ready', lambda: None)
    executor.process_comment(ledger.entry('reply'))
    assert not captured and ledger.entry('reply')['stage'] == 'approval_pending'
    payload['approval'] = {'reply_version': mod.comment_reply_version(payload)}
    ledger.update('reply', stage='ready', payload=payload)
    executor.verify_identity = lambda: ledger.set_state(mod.comment_settings_key(config), {**settings, 'enabled': False})
    executor.process_comment(ledger.entry('reply'))
    assert not captured and ledger.entry('reply')['stage'] == 'ready'
    ledger.set_state(mod.comment_settings_key(config), settings)
    executor.verify_identity = lambda: None
    executor.process_comment(ledger.entry('reply'))
    assert captured == [('note', '已生成', 'comment')]
    assert ledger.entry('reply')['stage'] == 'submitted'


@pytest.mark.parametrize('mode,human,expected', [('request_approval', False, 'approval_pending'), ('auto_approve', False, 'ready'), ('auto_approve', True, 'held'), ('full_access', False, 'ready')])
def test_comment_control_mode_generates_appropriate_state(tmp_path, mode, human, expected):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3'); config = {'route_id': 'route'}
    ledger.set_state(mod.comment_settings_key(config), {'enabled': True, 'auto_reply': False, 'control_mode': mode, 'route_id': 'route'})
    ledger.add('reply', 'comment', {'peer_id': 'reader'}); ledger.update('reply', stage='thinking')
    executor = mod.Executor(config, ledger, object(), object())
    executor.channel_result = lambda row: {'decision': 'handoff' if human else 'reply', 'text': '候选正文', 'requires_human': human}
    executor.process_comment(ledger.entry('reply'))
    assert ledger.entry('reply')['stage'] == expected


def test_changed_comment_route_or_mode_invalidates_old_proposal_and_approval(tmp_path):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3'); config = {'route_id': 'route'}
    original = mod.comment_settings(config, ledger.state)
    payload = {'peer_id': 'reader', 'reply': {'text': '旧回复'}, 'comment_policy': mod.comment_policy(original),
               'request': {'message_id': 'frozen'}, 'approval': {'reply_version': 'old'}}
    ledger.add('reply', 'comment', payload); ledger.update('reply', stage='ready', event_id='old-event')
    ledger.set_state(mod.comment_settings_key(config), {**original, 'route_id': 'new-route', 'control_mode': 'request_approval'})
    executor = mod.Executor(config, ledger, object(), object())
    executor.process_comment(ledger.entry('reply'))
    row = ledger.entry('reply'); payload = json.loads(row['payload'])
    assert row['stage'] == 'received' and not row['event_id']
    assert all(k not in payload for k in ('reply', 'request', 'approval')) and payload['request_version'] == 1
    assert payload['comment_policy'] == {'route_id': 'new-route', 'control_mode': 'request_approval'}


def test_additional_comment_account_runs_without_publishing_and_keeps_identity_isolated(tmp_path, monkeypatch):
    base = {'user_id': 1, 'pc_account_id': 2, 'expected_external_user_id': 'peach', 'route_id': 'route', 'enabled': False}
    account = SimpleNamespace(id=4, user_id=1, external_user_id='leo', nickname='Leo')
    config = mod.comment_account_config(base, account)
    assert config['comment_only'] and config['expected_external_user_id'] == 'leo'
    assert config['comment_visibility_external_user_id'] == 'peach'
    assert mod.comment_account_config(base, SimpleNamespace(**{**vars(account), 'user_id': 2})) is None
    path = mod.comment_ledger_path(config, tmp_path)
    assert path != mod.comment_ledger_path(base, tmp_path)
    replacement = {**config, 'expected_external_user_id': 'another-login'}
    assert mod.comment_ledger_path(replacement, tmp_path) != path
    ledger = mod.Ledger(path)
    executor = mod.Executor(config, ledger, object(), object())
    assert not mod.comment_settings(config, ledger.state)['enabled']
    settings = {**mod.comment_settings(config, ledger.state), 'enabled': True, 'started_at': mod.now().isoformat()}
    ledger.set_state(mod.comment_settings_key(config), settings)
    calls = []
    monkeypatch.setattr(mod.xhs_network, 'check', lambda: None)
    monkeypatch.setattr(mod.xhs_network, 'require_ready', lambda: None)
    executor.verify_identity = lambda: calls.append('identity')
    executor.poll_comments = lambda: calls.append('poll')
    executor.verify_comments = lambda: calls.append('verify')
    executor.tick_comments()
    assert calls == ['identity', 'poll', 'verify']
    assert ledger.state('execution') == 'active' and ledger.state('heartbeat')
    executor.assert_enabled()  # Does not consult the paused publishing autopilot.
    assert not ledger.rows('post')


@pytest.mark.parametrize('comment_only', [False, True])
def test_expired_login_preserves_ready_reply_and_resumes_without_duplicate_send(tmp_path, monkeypatch, comment_only):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    config = {'user_id': 1, 'pc_account_id': 2, 'expected_external_user_id': 'xiaotao', 'autopilot_id': 'test',
              'route_id': 'test-route', 'comment_only': comment_only, 'started_at': '2026-10-10T23:05:00+08:00'}
    ledger.set_state(mod.comment_settings_key(config), {'enabled': True, 'auto_reply': True, 'control_mode': 'full_access', 'route_id': 'test-route', 'started_at': config['started_at']})
    payload = mod.normalize_mention(notification(), 'xiaotao', {'xiaotao'})
    payload['reply'] = {'text': '谢谢你呀'}
    key = 'comment:2:' + payload['comment_id']
    ledger.add(key, 'comment', payload); ledger.update(key, stage='ready')
    executor = mod.Executor(config, ledger, SimpleNamespace(request=lambda *args: {'autopilot': {'status': 'active'}}), object())
    executor.schedule = executor.ingest_runs = executor.verify_posts = executor.verify_comments = executor.assert_enabled = lambda: None
    executor.notify = lambda *args: None
    monkeypatch.setattr(mod.xhs_network, 'check', lambda: None)
    monkeypatch.setattr(mod.xhs_network, 'require_ready', lambda: None)
    expired, sends = [True], []
    def pc():
        if expired[0]:
            raise mod.AccountUnavailable(2, 'expired')
        return SimpleNamespace(get_self_info=lambda: {'user_id': 'xiaotao', 'guest': False},
            get_mentions=lambda cursor: {'message_list': [notification()], 'has_more': False},
            post_comment=lambda *args: sends.append(args) or {'success': True, 'data': {'comment': {'id': 'sent'}}})
    executor.pc = pc
    tick = executor.tick_comments if comment_only else executor.tick
    tick()
    assert ledger.entry(key)['stage'] == 'ready' and not sends
    assert ledger.state('comment_health')['state'] == 'login_expired'
    assert mod.comment_settings(config, ledger.state)['enabled'] is True
    expired[0] = False
    tick(); tick()
    assert ledger.state('comment_health')['state'] == 'listening'
    assert ledger.entry(key)['stage'] == 'submitted'
    assert len(ledger.rows('comment')) == 1 and sends == [('note-a', '谢谢你呀', 'comment-a')]


def test_no_reply_reason_is_retained_for_workbench(tmp_path):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3'); ledger.add('c', 'comment', {'peer_id': 'reader'})
    ledger.update('c', stage='thinking')
    executor = mod.Executor({}, ledger, object(), object())
    executor.channel_result = lambda row: {'decision': 'no_reply', 'reason': '重复广告'}
    executor.process_comment(ledger.entry('c'))
    assert ledger.entry('c')['stage'] == 'no_reply'
    assert json.loads(ledger.entry('c')['payload'])['decision']['reason'] == '重复广告'


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


def test_review_uses_backend_provenance_without_requiring_body_credits(tmp_path):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    proposal = mod.validate_draft(draft())
    ledger.add('post', 'post', {'draft': proposal})
    ledger.update('post', stage='preparing')
    executor = mod.Executor({}, ledger, object(), object())
    captured = []
    executor.submit = lambda key, prompt, photos: captured.append((prompt, photos))
    executor.process_post(ledger.entry('post'))
    prompt, photos = captured[0]
    assert 'photos、sources后台字段' in prompt
    assert '不能仅因body没有来源说明而hold' in prompt
    assert '来源必须署名时核验其许可要求' in prompt
    assert '核对body里的摄影来源说明' not in prompt
    assert json.loads(prompt.split('最终稿：', 1)[1]) == proposal
    assert photos == proposal['photos']


def test_review_attachment_mapping_is_complete_and_frozen_on_retry(tmp_path):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    proposal = draft()
    ledger.add('post', 'post', {'draft': proposal})
    files = {}
    for photo in proposal['photos']:
        path = tmp_path / (photo['id'] + '.jpg')
        path.write_bytes(photo['id'].encode())
        files[photo['id']] = path
    uploads, calls = [], []
    def upload(account, blob, filename):
        uploads.append((account, filename))
        return {'id': 'attachment-' + str(len(uploads))}
    def send(account, **request):
        calls.append(request)
        return {'id': 'event'}
    channel = SimpleNamespace(upload=upload, send=send)
    executor = mod.Executor({'pc_account_id': 9, 'route_id': 'owned-route'}, ledger, object(), channel)
    executor.photo = lambda photo: files[photo['id']]
    executor.submit('post', 'review', proposal['photos'], {'review_photos': ['untrusted']})
    executor.submit('post', 'changed', list(reversed(proposal['photos'])))
    mapping = calls[0]['context']['review_photos']
    assert mapping == [{'photo_id': '7138819', 'asset_id': 'attachment-1'},
                       {'photo_id': '5596244', 'asset_id': 'attachment-2'}]
    assert calls[0] == calls[1] and len(uploads) == 2
    expected_ids = [photo['id'] for photo in proposal['photos']]
    assert mod.validate_review(review([entry['photo_id'] for entry in mapping]), expected_ids)
    assert not mod.validate_review(review(calls[0]['images']), expected_ids)


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


def test_post_reservation_has_no_daily_limit_and_prevents_reclaim(tmp_path):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    for key in ('a', 'b', 'c'):
        ledger.add(key, 'post', {})
        ledger.update(key, stage='ready')
    assert ledger.reserve_write('a')
    assert not ledger.reserve_write('a')
    ledger.update('a', stage='uncertain')
    assert ledger.reserve_write('b')
    ledger.update('b', stage='published')
    assert ledger.reserve_write('c')
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
    assert ledger.entry('reply')['stage'] == 'submitted'


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


@pytest.mark.parametrize('toast', ['平台提示原文', {'message': '平台提示原文', 'session': 'must-not-persist'}])
def test_comment_send_diagnostics_are_persisted_before_write_and_distinguish_egress(tmp_path, monkeypatch, toast):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    payload = mod.normalize_mention(notification(False), 'xiaotao', {'xiaotao'})
    payload['reply'] = {'text': '谢谢～'}
    ledger.add('reply', 'comment', payload); ledger.update('reply', stage='ready')
    executor = mod.Executor({'pc_account_id': 9}, ledger, object(), object())
    executor.verify_identity = lambda: None; executor.assert_enabled = lambda: None
    monkeypatch.setattr(mod.xhs_network, 'check', lambda: {'mode': 'home', 'label': 'home', 'state': 'online',
        'checked_at': mod.now().isoformat(), 'exit_ip': '2001:db8::1', 'proxy_password': 'must-not-persist'})
    monkeypatch.setattr(mod.xhs_network, 'require_ready', lambda: None)

    def post(*args):
        row = ledger.entry('reply')
        assert row['stage'] == 'sending'
        context = json.loads(row['payload'])['send_context']
        assert context['account_id'] == 9
        assert context['network_health']['exit_ip'] == '2001:db8::1'
        assert 'must-not-persist' not in row['payload']
        return {'success': True, 'msg': '提交回执原文', 'data': {'comment': {'id': 'sent', 'status': 2}, 'toast': toast},
                '_transport': {'platform_observed_exit_ip': '2001:db8::7', 'cookie': 'must-not-persist'}}

    executor.pc = lambda: SimpleNamespace(post_comment=post)
    executor.process_comment(ledger.entry('reply'))
    row = ledger.entry('reply'); stored = json.loads(row['payload'])
    assert row['stage'] == 'submitted'  # A successful ACK never proves recipient visibility.
    assert stored['receipt']['platform_status'] == 2
    assert stored['receipt']['platform_message'] == '提交回执原文'
    assert stored['receipt']['platform_toast'] == '平台提示原文'
    assert stored['receipt']['transport']['platform_observed_exit_ip'] == '2001:db8::7'
    assert stored['send_context']['network_health']['exit_ip'] == '2001:db8::1'
    assert 'must-not-persist' not in row['payload']


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
    from backend.app.models import User, PlatformAccount, AccountCookieVersion
    from backend.app.core.security import encrypt_text
    with sessions() as db:
        db.add(User(id=1, username='owner', password_hash='test'))
        db.add(PlatformAccount(id=3, user_id=1, platform='xhs', sub_type='creator', external_user_id='xiaotao', status='active'))
        db.add(AccountCookieVersion(platform_account_id=3, encrypted_cookies=encrypt_text('synthetic=1')))
        db.commit()
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


def test_sender_only_comment_is_not_delivered_but_new_comments_continue(tmp_path, monkeypatch):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    config = {'pc_account_id': 9, 'expected_external_user_id': 'xiaotao', 'comment_visibility_account_id': 1}
    executor = mod.Executor(config, ledger, object(), object())
    payload = mod.normalize_mention(notification(peer='leo'), 'xiaotao', {'xiaotao'})
    payload['raw']['item_info']['xsec_token'] = 'private-read-token'; payload['reply'] = {'text': '谢谢你呀😊'}
    ledger.add('sent', 'comment', payload); ledger.update('sent', stage='submitted', external_id='sent-a')
    reply = {'id': 'sent-a', 'note_id': 'note-a', 'content': '谢谢你呀😊', 'status': -1,
        'user_info': {'user_id': 'xiaotao'}, 'target_comment': {'id': 'comment-a'}}
    executor.pc = lambda: SimpleNamespace(get_note_comments=lambda url: (True, '', [{'id': 'comment-a', 'sub_comments': [reply]}]))
    visible = []
    executor.visibility_reader = lambda: (SimpleNamespace(get_note_comments=lambda url: (True, '', visible)), 'leo')
    executor.verify_comments()
    row = ledger.entry('sent'); verification = json.loads(row['payload'])['verification']
    assert row['stage'] == 'not_visible' and verification['sender_visible'] and not verification['observer_visible']
    assert verification['observer_role'] == 'recipient' and not ledger.state('comment_delivery')['blocked']
    assert ledger.state('comment_delivery')['has_visibility_issue']
    ledger.add('next', 'comment', payload); ledger.update('next', stage='ready')
    calls = []
    reader_pc = executor.pc
    executor.pc = lambda: SimpleNamespace(post_comment=lambda *args: calls.append(args) or {'success': True, 'data': {'comment': {'id': 'sent-next'}}})
    executor.verify_identity = lambda: None; executor.assert_enabled = lambda: None
    monkeypatch.setattr(mod.xhs_network, 'check', lambda: None); monkeypatch.setattr(mod.xhs_network, 'require_ready', lambda: None)
    executor.process_comment(ledger.entry('next'))
    executor.process_comment(ledger.entry('next'))
    assert len(calls) == 1 and ledger.entry('next')['stage'] == 'submitted'
    executor.pc = reader_pc
    # A later ordinary read can confirm moderation completion without resending.
    visible.append({**reply, 'status': 0}); ledger.set_state('comment_verify_at', 0); ledger.update('next', stage='no_reply')
    executor.verify_comments()
    assert ledger.entry('sent')['stage'] == 'replied' and not ledger.state('comment_delivery')['blocked']


def test_visibility_matching_rejects_wrong_target_author_and_content():
    reply = {'id': 'c', 'note_id': 'n', 'content': 'ok', 'user_info': {'user_id': 'peach'}, 'target_comment': {'id': 'parent'}}
    args = ('c', 'n', 'peach', 'parent', 'ok')
    assert mod.find_reply([reply], *args) is reply
    for key, value in [('note_id', 'other'), ('content', 'wrong'), ('user_info', {'user_id': 'other'}), ('target_comment', {'id': 'other'})]:
        assert mod.find_reply([{**reply, key: value}], *args) is None


def test_failed_read_never_means_recipient_hidden(tmp_path):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    payload = mod.normalize_mention(notification(), 'xiaotao', {'xiaotao'})
    payload['raw']['item_info']['xsec_token'] = 'private'; payload['reply'] = {'text': 'ok'}
    ledger.add('sent', 'comment', payload); ledger.update('sent', stage='submitted', external_id='c')
    executor = mod.Executor({'expected_external_user_id': 'xiaotao'}, ledger, object(), object())
    executor.pc = lambda: SimpleNamespace(get_note_comments=lambda url: (False, 'read failed', []))
    executor.visibility_reader = lambda: (SimpleNamespace(get_note_comments=lambda url: (True, '', [])), 'leo')
    with pytest.raises(mod.Paused): executor.verify_comments()
    assert ledger.entry('sent')['stage'] == 'submitted'


def test_comment_context_separates_unconfirmed_attempts_and_other_readers(tmp_path):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    current = mod.normalize_mention(notification(peer='leo'), 'xiaotao', {'xiaotao'})
    current['text'] = '看到请回复[微笑R]'
    for key, stage, changes in [
        ('confirmed', 'replied', {'verification': {'state': 'visible'}}),
        ('hidden', 'not_visible', {}), ('pending', 'submitted', {}),
        ('other-reader', 'replied', {'peer_id': 'somebody-else', 'verification': {'state': 'visible'}}),
        ('other-note', 'replied', {'note_id': 'another-note', 'verification': {'state': 'visible'}}),
        ('diagnostic:test', 'replied', {'verification': {'state': 'visible'}}),
        ('deleted-test', 'replied', {'verification': {'state': 'visible'}}),
    ]:
        prior = {**current, 'comment_id': key, 'reply': {'text': key}, **changes}
        ledger.add(key, 'comment', prior)
        ledger.update(key, stage=stage, external_id=key + '-reply',
                      detail='历史验收记录：测试已清理' if key == 'deleted-test' else '')
    ledger.add('current', 'comment', current)
    executor = mod.Executor({'route_id': 'r'}, ledger, object(), object())
    requests = []
    executor.submit = lambda key, prompt: requests.append((key, prompt))
    executor.process_comment(ledger.entry('current'))
    context = json.loads(requests[0][1].split('\n', 1)[1])
    assert [item['text'] for item in context['sent_replies']] == ['confirmed']
    assert {item['text'] for item in context['unconfirmed_replies']} == {'hidden', 'pending'}
    assert context['sent_replies'][0]['incoming_comment_id'] == 'confirmed'
    assert context['text'] == '看到请回复[微笑R]'
    assert '不代表对方收到' in requests[0][1]


@pytest.mark.parametrize('raises', [False, True])
def test_inaccessible_old_note_does_not_block_new_reply_verification(tmp_path, raises):
    ledger = mod.Ledger(tmp_path / 'state.sqlite3')
    executor = mod.Executor({'pc_account_id': 9, 'expected_external_user_id': 'xiaotao',
                            'comment_visibility_account_id': 1}, ledger, object(), object())
    payload = mod.normalize_mention(notification(peer='leo'), 'xiaotao', {'xiaotao'})
    payload['raw']['item_info']['xsec_token'] = 'read-token'
    payload['reply'] = {'text': '收到'}
    for key in ('old', 'new'):
        ledger.add(key, 'comment', {**payload, 'note_id': key})
        ledger.update(key, stage='submitted', external_id=key + '-reply')
    reply = {'id': 'new-reply', 'note_id': 'new', 'content': '收到', 'status': 0,
             'user_info': {'user_id': 'xiaotao'}, 'target_comment': {'id': 'comment-a'}}
    calls = []
    def read(url):
        calls.append(url)
        if '/old?' in url:
            if raises: raise RuntimeError('incomplete response')
            return (False, 'failed', [])
        return (True, '', [reply])
    executor.pc = lambda: SimpleNamespace(get_note_comments=read)
    executor.visibility_reader = lambda: (SimpleNamespace(get_note_comments=read), 'leo')
    with pytest.raises(mod.Paused):
        executor.verify_comments()
    assert ledger.entry('old')['stage'] == 'submitted'
    assert ledger.entry('new')['stage'] == 'replied'
    assert json.loads(ledger.entry('new')['payload'])['verification']['observer_visible'] is True
    assert any('/new?' in url for url in calls)


def test_visibility_reader_cannot_use_another_owners_account(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    database = MagicMock(); database.__enter__.return_value = database
    database.get.return_value = SimpleNamespace(user_id=2, platform='xhs', sub_type='pc', status='active', external_user_id='leo')
    monkeypatch.setattr(mod, 'SessionLocal', lambda: database)
    executor = mod.Executor({'user_id': 1, 'expected_external_user_id': 'xiaotao', 'comment_visibility_account_id': 1, 'comment_visibility_external_user_id': 'leo'}, mod.Ledger(tmp_path / 'state.sqlite3'), object(), object())
    with pytest.raises(mod.Paused): executor.visibility_reader()
    database.scalar.assert_not_called()


def test_publisher_preflight_uses_bound_structured_evidence_and_deduplicates(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.app.core.database import Base
    from backend.app.models import User, PlatformAccount, AccountCookieVersion
    from backend.app.models.api_log import ApiLog
    engine=create_engine('sqlite:///'+str(tmp_path/'preflight.db'))
    Base.metadata.create_all(engine);sessions=sessionmaker(bind=engine)
    with sessions() as db:
        db.add(User(id=1,username='owner',password_hash='test'))
        db.add(PlatformAccount(id=3,user_id=1,platform='xhs',sub_type='creator',external_user_id='owned',status='risk'))
        db.add(AccountCookieVersion(platform_account_id=3,encrypted_cookies='synthetic'))
        db.add(ApiLog(user_id=1,platform='xhs',endpoint='internal/xhs/account-session',status='risk',
                     meta={'account_id':3,'external_id':'owned','source':'scheduled_check','business_code':906}))
        db.commit()
    monkeypatch.setattr(mod,'SessionLocal',sessions)
    executor=mod.Executor({'user_id':1,'creator_account_id':3,'expected_external_user_id':'owned'},
        mod.Ledger(tmp_path/'preflight.sqlite3'),object(),object())
    executor.report_publisher_preflight('post:today');executor.report_publisher_preflight('post:today')
    with sessions() as db:
        logs=db.query(ApiLog).all()
        assert len(logs)==2
        meta=logs[-1].meta
        assert meta['source']=='publisher_preflight' and meta['component']=='publisher'
        assert meta['operation_key']=='post:today' and meta['business_code']==906
    executor.config['expected_external_user_id']='different'
    executor.report_publisher_preflight('post:other')
    with sessions() as db:assert db.query(ApiLog).count()==2


def test_creator_risk_keeps_reviewed_queue_then_new_login_resumes_once(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.app.core.database import Base
    from backend.app.models import User, PlatformAccount, AccountCookieVersion
    from backend.app.core.security import encrypt_text
    from backend.app.adapters.xhs.creator_login_adapter import XhsCreatorLoginAdapter
    from backend.app.services.account_health import CreatorUploadError
    engine = create_engine('sqlite:///' + str(tmp_path / 'queue.db'))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add(User(id=1, username='owner', password_hash='test'))
        db.add(PlatformAccount(id=3, user_id=1, platform='xhs', sub_type='creator', external_user_id='xiaotao', status='active'))
        db.add(AccountCookieVersion(platform_account_id=3, encrypted_cookies=encrypt_text('synthetic=old')))
        db.commit()
    monkeypatch.setattr(mod, 'SessionLocal', sessions)
    monkeypatch.setattr(mod.xhs_network, 'check', lambda: None)
    monkeypatch.setattr(mod.xhs_network, 'require_ready', lambda: None)
    monkeypatch.setattr(XhsCreatorLoginAdapter, 'get_user_info', lambda *a: {'external_user_id': 'xiaotao'})
    value = mod.validate_draft(draft())
    images = {}
    for n, photo in enumerate(value['photos']):
        images[photo['id']] = tmp_path / (photo['id'] + '.jpg')
        images[photo['id']].write_bytes(b'synthetic-image' + bytes([n]))
    hashes = {pid: mod.hashlib.sha256(p.read_bytes()).hexdigest() for pid, p in images.items()}
    payload = {'draft': value, 'review': review(list(images)),
        'review_input_fingerprint': mod.hashlib.sha256(mod.dump([value, hashes]).encode()).hexdigest()}
    ledger = mod.Ledger(tmp_path / 'queue.sqlite3')
    ledger.add('post', 'post', payload); ledger.update('post', stage='ready')
    executor = mod.Executor({'user_id': 1, 'creator_account_id': 3, 'expected_external_user_id': 'xiaotao'},
        ledger, SimpleNamespace(request=lambda *a: {}), object())
    executor.photo = lambda p: images[p['id']]
    executor.verify_identity = executor.assert_enabled = executor.refresh_context = lambda: None
    notifications, uploads, posts = [], [], []
    executor.notify = lambda *args: notifications.append(args)
    def upload(*args):
        uploads.append(args)
        if executor.account('creator') == 'synthetic=old':
            raise CreatorUploadError({'phase': 'permit', 'http_status': 401, 'business_code': 906})
        return {'fileIds': 'synthetic-media', 'width': 800, 'height': 1000}
    executor.creator = lambda: SimpleNamespace(cookies=executor.account('creator'), refreshed_cookies=None,
        upload_media=upload, post_note=lambda data: posts.append(data) or {'data': {'id': 'a' * 24}})
    with pytest.raises(mod.Paused, match='906'):
        executor.publish(ledger.entry('post'))
    assert ledger.entry('post')['stage'] == 'ready'
    job_id = ledger.entry('post')['job_id']
    with sessions() as db:
        assert db.get(PlatformAccount, 3).status == 'risk'
        assert '906' in db.get(mod.PublishJob, job_id).publish_error
        assert all('906' in a.upload_error or not a.upload_error for a in db.query(mod.PublishAsset).all())
    # No repeated permit or POST while the same credentials require verification.
    with pytest.raises(mod.Paused, match='906'):
        executor.publish(ledger.entry('post'))
    assert len(uploads) == 1 and not posts
    with sessions() as db:
        db.add(AccountCookieVersion(platform_account_id=3, encrypted_cookies=encrypt_text('synthetic=new')))
        db.get(PlatformAccount, 3).status = 'active'
        db.get(PlatformAccount, 3).status_message = ''
        db.commit()
    executor.process_post(ledger.entry('post'))
    executor.process_post(ledger.entry('post'))
    assert ledger.entry('post')['stage'] == 'published'
    assert ledger.entry('post')['job_id'] == job_id
    assert ledger.entry('post')['detail'] == ''
    assert len(posts) == 1 and len(uploads) == 3
    with sessions() as db:
        assert db.get(mod.PublishJob, job_id).publish_error == ''
        assert all(a.upload_error == '' for a in db.query(mod.PublishAsset).all())
    engine.dispose()


def test_agent_owned_publishing_preserves_user_automation_prompt(tmp_path):
    ledger = mod.Ledger(tmp_path / 'native.sqlite3')
    executor = mod.Executor({'post_execution': 'agent'}, ledger, object())
    executor.refresh_context()
    ledger.db.close()


def test_native_publish_recovery_runs_before_comment_network_io(tmp_path,monkeypatch):
    from backend.app.services import native_publish_tracking
    ledger=mod.Ledger(tmp_path/'order.sqlite3');ledger.add('post:test','post',{},'issue')
    calls=[]
    monkeypatch.setattr(native_publish_tracking,'reconcile',lambda *a:calls.append('publish'))
    remote=SimpleNamespace(request=lambda *a:{'autopilot':{'status':'active'}})
    executor=mod.Executor({'post_execution':'agent','autopilot_id':'auto'},ledger,remote)
    executor.schedule=executor.ingest_runs=executor.verify_comments=lambda:None
    executor.poll_comments=lambda:calls.append('comments')
    executor.tick()
    assert calls==['publish','comments']
    ledger.db.close()
