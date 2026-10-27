"""Search comments and proactive IM use only synthetic notes and sockets."""
import asyncio
import json
import sqlite3
from collections import Counter
from datetime import timedelta, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from test_proactive_interactions import setup, Platform, configure, start, state, headers
from backend.app.models import PlatformAccount
from backend.app.models.direct_message import DmManualMessage, DmListener, DmHistory
from backend.app.services import proactive_interactions as service, dm_chat, dm_worker


@pytest.fixture
def discovery(setup, monkeypatch):
    client, sessions = setup
    searches = []
    def search(self, keyword, page=1):
        searches.append((keyword, page))
        return True, '', {'data': {'items': [self.card(f'n{i}', f'peer{i}') for i in range(4)]}}
    def detail(self, url):
        note = url.split('/')[-1].split('?')[0]
        return True, '', {'data': {'items': [self.card(note, f'peer{note[1:]}')]}}
    def decide(config, context):
        note = context['notes'][0]['note_id']
        number = len(context.get('already_generated', {}).get(note, [])) + 1
        return {'note_id': note, 'text': f'关于这篇笔记的具体问题 {number}', 'reason': '根据正文交流'}
    monkeypatch.setattr(Platform, 'search_note', search, raising=False)
    monkeypatch.setattr(Platform, 'get_note_info', detail)
    monkeypatch.setattr(service, 'decide', decide)
    monkeypatch.setattr(dm_worker, 'SessionLocal', sessions)
    return client, sessions, searches


def deliver(sessions, *, code=0, acknowledge=True):
    pending, writes = {}, []
    class Socket:
        async def send(self, peer, text, mid):
            writes.append((peer, text))
            if not acknowledge:
                raise TimeoutError('synthetic lost acknowledgment')
            pending[mid].set_result({'mid': mid, 'code': code, 'msg': 'ok' if code == 0 else 'rejected',
                                    'messageid': 'receipt-' + mid})
    sent = asyncio.run(dm_worker.deliver_manual(1, Socket(), pending, 'self'))
    return sent, writes


def test_search_x_notes_sends_y_distinct_comments_on_each(discovery):
    client, _, searches = discovery
    start(client, keyword='露营', browse_limit=3, comments_per_note=2, comment_limit=1)
    service.tick()
    assert searches == [('露营', 1)]
    assert Counter(note for note, _ in Platform.writes) == {'n0': 2, 'n1': 2, 'n2': 2}
    result = state(client)
    assert len(result['items']) == 6 and all(i['state'] == 'submitted' for i in result['items'])
    assert {i['sequence'] for i in result['items']} == {1, 2}
    assert all(len({text for note, text in Platform.writes if note == target}) == 2 for target in ('n0', 'n1', 'n2'))
    start(client, keyword='露营', browse_limit=3, comments_per_note=2); service.tick()
    assert Counter(note for note, _ in Platform.writes) == {'n0': 2, 'n1': 2, 'n2': 2, 'n3': 2}
    start(client, keyword='露营', browse_limit=3, comments_per_note=2); service.tick()
    assert len(Platform.writes) == 8  # Exhausted page cannot replay previously contacted notes.


def test_each_search_note_keeps_its_quota_even_for_same_author(discovery, monkeypatch):
    client, _, _ = discovery
    monkeypatch.setattr(Platform, 'search_note', lambda *args, **kwargs: (True, '', {'data': {'items': [Platform.card('a', 'peer'), Platform.card('b', 'peer')]}}))
    monkeypatch.setattr(Platform, 'get_note_info', lambda self, url: (True, '', {'data': {'items': [Platform.card(url.split('/')[-1].split('?')[0], 'peer')]}}))
    start(client, keyword='露营', browse_limit=2, comments_per_note=2); service.tick()
    assert Counter(note for note, _ in Platform.writes) == {'a': 2, 'b': 2}


def test_repeated_model_comment_is_not_posted_twice(discovery, monkeypatch):
    client, _, _ = discovery
    monkeypatch.setattr(service, 'decide', lambda config, context: {'note_id': context['notes'][0]['note_id'], 'text': '同一个问题'})
    start(client, keyword='露营', browse_limit=2, comments_per_note=3); service.tick()
    assert Counter(note for note, _ in Platform.writes) == {'n0': 1, 'n1': 1}


def test_setting_change_between_comments_stops_remaining_posts(discovery, monkeypatch):
    client, _, _ = discovery
    original = service.decide
    count = 0
    def decide(config, context):
        nonlocal count
        count += 1
        if count == 2:
            configure(client, keyword='新主题', comments_per_note=3)
        return original(config, context)
    monkeypatch.setattr(service, 'decide', decide)
    start(client, keyword='露营', browse_limit=2, comments_per_note=3); service.tick()
    assert len(Platform.writes) == 1
    assert state(client)['runs'][0]['state'] == 'cancelled'


@pytest.mark.parametrize('share_note', [True, False])
def test_private_discovery_uses_existing_worker_and_actual_acknowledgments(discovery, share_note):
    client, sessions, searches = discovery
    start(client, source='outreach', keyword='露营', browse_limit=3, dm_limit=2, share_note=share_note)
    service.tick()
    result = state(client)
    assert searches == [('露营', 1)] and not Platform.writes
    assert result['runs'][0]['state'] == 'waiting_delivery'
    assert all(item['state'] == 'pending_delivery' and not item.get('sent_at') for item in result['items'])
    with sessions() as db:
        rows = list(db.scalars(select(DmManualMessage).order_by(DmManualMessage.id)))
        assert len(rows) == (4 if share_note else 2)
        assert all(row.peer_id in ('peer0', 'peer1') for row in rows)
        assert db.get(DmListener, 1).auto_reply is False
        if share_note:
            # The text cannot overtake the queued note link.
            assert dm_chat.claim(db, rows[1].id, 'self') is None
    writes = []
    for _ in range(4 if share_note else 2):
        did_send, part = deliver(sessions)
        assert did_send
        writes += part
    assert deliver(sessions) == (False, [])
    service.tick()
    result = state(client)
    assert result['runs'][0]['state'] == 'completed'
    assert len(result['items']) == 2 and all(i['state'] == 'submitted' for i in result['items'])
    assert all(len(i['messages']) == (2 if share_note else 1) for i in result['items'])
    assert all(i['sent_at'] and i['external_id'] for i in result['items'])
    if share_note:
        assert '/n0' in writes[0][1] and not writes[1][1].startswith('https://')
        assert '/n1' in writes[2][1] and not writes[3][1].startswith('https://')
    else:
        assert all(not text.startswith('https://') for _, text in writes)
    with sessions() as db:
        assert len(list(db.scalars(select(DmHistory)))) == len(writes)
    start(client, source='outreach', keyword='露营', browse_limit=2, dm_limit=2, share_note=share_note)
    service.tick()
    assert state(client)['runs'][0]['state'] == 'waiting_delivery'
    additional = []
    for _ in range(4 if share_note else 2):
        did_send, part = deliver(sessions)
        assert did_send
        additional += part
    assert {peer for peer, _ in additional} == {'peer2', 'peer3'}
    service.tick()
    start(client, source='outreach', keyword='露营', browse_limit=2, dm_limit=2, share_note=share_note)
    service.tick()
    assert state(client)['runs'][0]['state'] == 'skipped'
    assert deliver(sessions) == (False, [])


@pytest.mark.parametrize('acknowledge,expected', [(True, 'failed'), (False, 'uncertain')])
def test_failed_or_uncertain_share_never_sends_text_or_retries(discovery, acknowledge, expected):
    client, sessions, _ = discovery
    start(client, source='outreach', keyword='露营'); service.tick()
    assert deliver(sessions, code=403, acknowledge=acknowledge)[0]
    service.tick()
    item = state(client)['items'][0]
    assert item['state'] == expected and item['messages'][1]['state'] == 'cancelled'
    for _ in range(2):
        assert deliver(sessions) == (False, [])
        service.tick()


def test_partial_success_is_reported_without_replaying_note(discovery):
    client, sessions, _ = discovery
    start(client, source='outreach', keyword='露营'); service.tick()
    assert deliver(sessions)[0]
    assert deliver(sessions, code=403)[0]
    service.tick()
    result = state(client)
    assert result['runs'][0]['state'] == result['items'][0]['state'] == 'partial'
    assert [m['state'] for m in result['items'][0]['messages']] == ['sent', 'failed']
    assert deliver(sessions) == (False, [])


@pytest.mark.parametrize('after_note', [False, True])
def test_disable_or_edit_strategy_rechecks_before_each_message(discovery, after_note):
    client, sessions, _ = discovery
    start(client, source='outreach', keyword='露营'); service.tick()
    if after_note:
        assert deliver(sessions)[0]
    configure(client, source='outreach', keyword='露营', enabled=False, share_note=False)
    assert deliver(sessions) == (False, [])
    assert deliver(sessions) == (False, [])
    service.tick()
    assert state(client)['items'][0]['state'] == ('partial' if after_note else 'cancelled')


@pytest.mark.parametrize('change', ['stop', 'manual', 'identity'])
def test_last_moment_stop_takeover_or_identity_change_blocks_private_send(discovery, monkeypatch, change):
    client, sessions, _ = discovery
    start(client, source='outreach', keyword='露营'); service.tick()
    if change == 'stop':
        monkeypatch.setattr(service, 'stopped_peers', lambda account: {'peer0'})
    else:
        with sessions() as db:
            if change == 'identity':
                db.get(PlatformAccount, 1).external_user_id = 'someone-else'
            else:
                dm_chat.set_control(db, 1, 'peer0', True)
            db.commit()
    assert deliver(sessions) == (False, [])
    assert deliver(sessions) == (False, [])
    service.tick()
    assert state(client)['items'][0]['state'] in ('failed', 'cancelled')


def test_comment_and_dm_for_same_note_have_independent_deduplication(discovery):
    client, sessions, _ = discovery
    start(client, keyword='露营', browse_limit=1); service.tick()
    start(client, source='outreach', keyword='露营', browse_limit=1); service.tick()
    assert len(Platform.writes) == 1
    assert deliver(sessions)[0] and deliver(sessions)[0]
    service.tick()
    assert {item['interaction'] for item in state(client)['items']} == {'comment', 'dm'}
    assert all(item['state'] == 'submitted' for item in state(client)['items'])


def test_private_queue_waits_for_verified_account_recovery(discovery):
    client, sessions, _ = discovery
    start(client, source='outreach', keyword='露营', share_note=False); service.tick()
    with sessions() as db:
        db.get(PlatformAccount, 1).status = 'expired'; db.commit()
    assert deliver(sessions) == (False, [])
    service.tick()
    assert state(client)['items'][0]['state'] == 'pending_delivery'
    with sessions() as db:
        db.get(PlatformAccount, 1).status = 'active'; db.commit()
    assert deliver(sessions)[0]
    service.tick()
    assert state(client)['items'][0]['state'] == 'submitted'


def test_restart_during_share_preserves_uncertainty_and_cancels_text(discovery):
    client, sessions, _ = discovery
    start(client, source='outreach', keyword='露营'); service.tick()
    with sessions() as db:
        first = db.scalars(select(DmManualMessage).order_by(DmManualMessage.id)).first()
        assert dm_chat.claim(db, first.id, 'self')
    dm_worker.recover()
    service.tick()
    item = state(client)['items'][0]
    assert item['state'] == 'uncertain'
    assert [m['state'] for m in item['messages']] == ['uncertain', 'cancelled']
    assert deliver(sessions) == (False, [])


def test_search_failure_is_not_reported_as_successful_empty_round(discovery, monkeypatch):
    client, _, _ = discovery
    monkeypatch.setattr(Platform, 'search_note', lambda *args, **kwargs: (False, 'private-do-not-show', {}))
    start(client, keyword='露营'); service.tick()
    assert state(client)['runs'][0]['state'] == 'failed'
    assert not Platform.writes and 'private-do-not-show' not in str(state(client))


def test_new_settings_validate_and_preserve_existing_strategy(discovery):
    client, _, _ = discovery
    old = configure(client, source='recommend', enabled=True)
    assert state(client)['strategies']['outreach']['enabled'] is False
    for body in ({'keyword': ' '}, {'keyword': '露营', 'comments_per_note': 0},
                 {'keyword': '露营', 'dm_limit': 11}, {'keyword': '露营', 'share_note': 'yes'}):
        assert client.put('/api/xhs/proactive/1/settings/outreach', json=body, headers=headers()).status_code == 422
    configure(client, source='outreach', keyword='露营', share_note=False)
    assert state(client)['strategies']['recommend']['revision'] == old['revision']


def test_legacy_ledger_migration_preserves_receipts_and_does_not_activate_dm(discovery):
    client, _, _ = discovery
    start(client); service.tick()
    original = state(client)['items'][0]
    with sqlite3.connect(service.STORE) as db:
        db.executescript('''ALTER TABLE items RENAME TO items_new;
        CREATE TABLE items(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,account_id INTEGER NOT NULL,
          identity TEXT NOT NULL,note_id TEXT NOT NULL,author_id TEXT NOT NULL,body TEXT NOT NULL,
          state TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',external_id TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,UNIQUE(account_id,identity,note_id));
        INSERT INTO items SELECT id,run_id,account_id,identity,note_id,author_id,body,state,detail,external_id,created_at FROM items_new;
        DROP TABLE items_new;''')
    migrated = state(client)
    assert migrated['items'][0] == original
    assert migrated['strategies']['outreach']['enabled'] is False
    with service.store() as db:
        assert db.execute('SELECT interaction,sequence FROM items').fetchone()[:] == ('comment', 0)


def test_new_defaults_and_all_strategy_preferences(discovery):
    client, _, _ = discovery
    for config in state(client)['strategies'].values():
        assert config['browse_limit'] == 3 and config['comments_per_note'] == 1
        assert config['daily_start'] == '00:00' and config['daily_end'] == '23:59'
    configure(client, source='visitors', instruction='保留回访偏好')
    assert state(client)['strategies']['visitors']['instruction'] == '保留回访偏好'
    for field, value in [('daily_start', '9:00'), ('daily_start', '24:00'), ('daily_end', '09:60')]:
        response = client.put('/api/xhs/proactive/1/settings/recommend', json={field: value}, headers=headers())
        assert response.status_code == 422


def test_daily_schedule_starts_at_opening_and_never_queues_outside(discovery, monkeypatch):
    client, _, _ = discovery
    base = service.shanghai_now().replace(hour=8, minute=30, second=0, microsecond=0)
    clock = [base]
    monkeypatch.setattr(service, 'shanghai_now', lambda: clock[0])
    saved = configure(client, enabled=True, daily_start='09:00', daily_end='10:00', interval_minutes=60)
    assert datetime.fromisoformat(saved['next_at']) == base.replace(hour=9, minute=0)
    fired = []
    def execute(run):
        fired.append(clock[0]); service.finish(run['id'], 'completed', 'synthetic')
    monkeypatch.setattr(service, 'execute', execute)
    with service.store() as db:
        db.execute("UPDATE strategy_settings SET next_at='2000-01-01'")
    service.tick()
    assert not fired
    clock[0] = base.replace(hour=9, minute=0); service.tick()
    assert len(fired) == 1
    clock[0] = base.replace(hour=10, minute=1); service.tick()
    assert len(fired) == 1
    clock[0] = (base + timedelta(days=1)).replace(hour=9, minute=0); service.tick()
    assert len(fired) == 2


def test_daily_window_can_cross_midnight_and_uses_each_strategy(discovery, monkeypatch):
    client, _, _ = discovery
    base = service.shanghai_now()
    config = {'daily_start': '22:00', 'daily_end': '06:00', 'interval_minutes': 60}
    for hour, minute, expected in [(21, 59, False), (22, 0, True), (23, 59, True), (0, 0, True), (6, 0, True), (6, 1, False)]:
        assert service.within_window(config, base.replace(hour=hour, minute=minute)) is expected
    monkeypatch.setattr(service, 'shanghai_now', lambda: base.replace(hour=12, minute=0))
    configure(client, source='visitors', daily_start='09:00', daily_end='18:00')
    configure(client, source='outreach', keyword='露营', **config)
    assert state(client)['strategies']['visitors']['daily_end'] == '18:00'
    assert client.post('/api/xhs/proactive/1/run/outreach', headers=headers()).status_code == 409
    assert not state(client)['runs']


def test_queued_round_waits_until_next_daily_window(discovery, monkeypatch):
    client, _, _ = discovery
    base = service.shanghai_now().replace(hour=9, minute=59, second=0, microsecond=0)
    clock = [base]
    monkeypatch.setattr(service, 'shanghai_now', lambda: clock[0])
    start(client, keyword='露营', browse_limit=1, daily_start='09:00', daily_end='10:00')
    clock[0] = base.replace(hour=10, minute=1); service.tick()
    assert state(client)['runs'][0]['state'] == 'waiting_window' and not Platform.writes
    clock[0] = (base + timedelta(days=1)).replace(hour=9, minute=0); service.tick()
    assert state(client)['runs'][0]['state'] == 'completed' and len(Platform.writes) == 1


def test_comment_generated_after_daily_end_is_not_sent(discovery, monkeypatch):
    client, _, _ = discovery
    base = service.shanghai_now().replace(hour=9, minute=59, second=0, microsecond=0)
    clock = [base]
    monkeypatch.setattr(service, 'shanghai_now', lambda: clock[0])
    original = service.decide
    def decide(config, context):
        result = original(config, context)
        clock[0] = base.replace(hour=10, minute=1)
        return result
    monkeypatch.setattr(service, 'decide', decide)
    start(client, keyword='露营', daily_start='09:00', daily_end='10:00'); service.tick()
    result = state(client)
    assert not Platform.writes and result['items'][0]['state'] == 'cancelled'
    assert '结束时间' in result['runs'][0]['detail']


def test_private_text_pauses_at_daily_end_and_resumes_next_opening(discovery, monkeypatch):
    client, sessions, _ = discovery
    base = service.shanghai_now().replace(hour=9, minute=59, second=0, microsecond=0)
    clock = [base]
    monkeypatch.setattr(service, 'shanghai_now', lambda: clock[0])
    start(client, source='outreach', keyword='露营', daily_start='09:00', daily_end='10:00'); service.tick()
    assert deliver(sessions)[0]
    clock[0] = base.replace(hour=10, minute=1)
    assert deliver(sessions) == (False, [])
    service.tick()
    assert state(client)['items'][0]['state'] == 'pending_delivery'
    clock[0] = (base + timedelta(days=1)).replace(hour=9, minute=0)
    assert deliver(sessions)[0]
    service.tick()
    assert state(client)['items'][0]['state'] == 'submitted'


@pytest.fixture
def agents(monkeypatch):
    def route(name, owner='1'):
        return {'id': 'route-' + name, 'version': 2, 'agent': {'id': name, 'name': '助手 ' + name},
                'categories': ['lifestyle_editor'], 'surfaces': ['manual'],
                'event_types': ['manual.request'], 'kinds': ['text'], 'account_ids': [owner]}
    catalog = {'connected': True, 'agents': [{'id': name, 'name': '助手 ' + name} for name in ('a', 'b')],
               'routes': [route('a'), route('b')]}
    monkeypatch.setattr(service.routing, 'channel_client', lambda: SimpleNamespace(catalog=lambda: catalog))
    return catalog, route


def test_strategy_agent_selection_is_owned_independent_and_used_for_generation(discovery, agents, monkeypatch):
    from backend.app.models import ModelConfig
    client, sessions, _ = discovery
    assert client.get('/api/xhs/proactive/1/agents').status_code == 401
    assert client.get('/api/xhs/proactive/1/agents', headers=headers(2)).status_code == 404
    choices = client.get('/api/xhs/proactive/1/agents', headers=headers()).json()
    assert {a['id'] for a in choices['agents']} == {'a', 'b'}
    configure(client, source='visitors', agent_id='b')
    seen = []
    original = service.decide
    def decide(config, context):
        seen.append(config.model_name)
        return original(config, context)
    monkeypatch.setattr(service, 'decide', decide)
    start(client, keyword='露营', browse_limit=1, agent_id='a'); service.tick()
    result = state(client)
    assert seen == ['route-a'] and result['items'][0]['agent_snapshot']['id'] == 'a'
    assert result['strategies']['visitors']['agent_id'] == 'b'
    assert result['strategies']['recommend']['agent_id'] == 'a'
    with sessions() as db:
        assert not list(db.scalars(select(ModelConfig)))  # No unrelated account route is overwritten.


def test_deleted_explicit_agent_never_falls_back_to_account_default(discovery, agents):
    client, _, _ = discovery
    catalog, _ = agents
    start(client, keyword='露营', browse_limit=1, agent_id='a')
    catalog['agents'] = [a for a in catalog['agents'] if a['id'] != 'a']
    service.tick()
    assert not Platform.writes and not state(client)['items']
    assert state(client)['runs'][0]['state'] == 'failed'
    assert '重新选择' in state(client)['runs'][0]['detail']


def test_invalid_agent_route_is_rejected_and_stopping_works_when_catalog_is_down(discovery, agents):
    client, _, _ = discovery
    catalog, route = agents
    configure(client, enabled=True, agent_id='a')
    catalog['connected'] = False
    stopped = configure(client, enabled=False, agent_id='a')
    assert stopped['agent_snapshot']['id'] == 'a' and stopped['enabled'] is False
    assert client.put('/api/xhs/proactive/1/settings/recommend', json={'agent_id': 'b'}, headers=headers()).status_code == 503
    catalog['connected'] = True
    catalog['routes'][1]['enabled'] = False
    assert client.put('/api/xhs/proactive/1/settings/recommend', json={'agent_id': 'b'}, headers=headers()).status_code == 400
    catalog['routes'][1]['enabled'] = True
    catalog.pop('agents')
    catalog['routes'].append(route('other', owner='2'))
    assert client.put('/api/xhs/proactive/1/settings/recommend', json={'agent_id': 'other'}, headers=headers()).status_code == 400
    assert state(client)['strategies']['recommend']['agent_id'] == 'a'


def test_private_discovery_uses_its_separate_agent_and_records_snapshot(discovery, agents, monkeypatch):
    client, sessions, _ = discovery
    configure(client, keyword='露营', agent_id='a')
    seen = []
    original = service.decide
    def decide(config, context):
        seen.append((config.model_name, context['interaction']))
        return original(config, context)
    monkeypatch.setattr(service, 'decide', decide)
    start(client, source='outreach', keyword='露营', agent_id='b', share_note=False); service.tick()
    assert seen == [('route-b', 'dm')]
    assert deliver(sessions)[0]; service.tick()
    assert state(client)['items'][0]['agent_snapshot']['id'] == 'b'
