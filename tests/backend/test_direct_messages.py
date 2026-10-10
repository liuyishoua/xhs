"""Synthetic transports only: no real social messages or model executions."""
from datetime import timedelta
from types import SimpleNamespace
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from backend.app.core.database import Base, get_db
from backend.app.core.deps import get_current_user
from backend.app.core.time import shanghai_now
from backend.app.models import User, PlatformAccount, ModelConfig
from backend.app.models.direct_message import DirectMessage, DmListener
from backend.app.services import dm_service as dm
from backend.app.api import direct_messages, model_routing


@pytest.fixture
def database(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'test.db'))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all([User(id=1, username='owner', password_hash='test'), User(id=2, username='other', password_hash='test')])
        db.add_all([PlatformAccount(id=1, user_id=1, platform='xhs', sub_type='pc', external_user_id='own'),
                    PlatformAccount(id=2, user_id=2, platform='xhs', sub_type='pc', external_user_id='other')])
        db.add(DmListener(account_id=1, enabled=True, auto_reply=True, route_id='route-a', started_at=shanghai_now()-timedelta(minutes=1)))
        db.commit()
    yield factory
    engine.dispose()


def event(text='合成测试咨询', **kwargs):
    body = dict(sender='customer-a', receiver='own', content=text, contentType=1, groupChat=False)
    body.update(kwargs)
    return {'chatMessage': {'messageid': 'platform-id', 'mid': 'transport-id', 'payload_json': body}}


def empty_history(peer, **kwargs):
    return {'result': {'success': True}, 'out_message_list': []}


def accept(db, prepared=True):
    row = dm.ingest(db, db.get(PlatformAccount, 1), db.get(DmListener, 1), event())
    if prepared:
        dm.prepare_history(db, row, empty_history)
    return row


class FakeChannel:
    def __init__(self):
        self.calls = []
        self.drop_response = False
        self.output = {'text': 'raw', 'structured': {'reply_text': '合成测试回复', 'requires_human': False}}
    def send(self, **kwargs):
        self.calls.append(kwargs)
        if self.drop_response:
            self.drop_response = False
            raise ValueError('lost response')
        return {'id': 'channel-event'}
    def status(self, event_id, account_id):
        assert event_id == 'channel-event'
        assert account_id == 'user:1:account:own'
        return {'state': 'succeeded', 'result': {'output': self.output}}


def test_intake_duplicate_conflict_and_scope(database):
    with database() as db:
        first = accept(db)
        assert first.id == accept(db).id
        assert first.channel_payload['account_id'] == 'user:1:account:own'
        assert first.channel_payload['conversation_id'] == 'customer-a'
        assert first.channel_payload['surface'] == 'dm'
        with pytest.raises(ValueError, match='冲突'):
            dm.ingest(db, db.get(PlatformAccount, 1), db.get(DmListener, 1), event('changed'))
        assert len(db.scalars(select(DirectMessage)).all()) == 1


@pytest.mark.parametrize('kwargs', [{'sender': 'own'}, {'receiver': 'other'}, {'groupChat': True}, {'contentType': 2}, {'sender': {}}, {'content': {'image': 'url'}}])
def test_ignore_echo_wrong_recipient_groups_and_nontext(kwargs):
    assert dm.parse_inbound(event(**kwargs), 'own') is None


def test_nested_text_and_history_cutoff(database):
    assert dm.parse_inbound(event(json.dumps({'content': json.dumps({'text': '文字'})})), 'own')['text'] == '文字'
    with database() as db:
        old = event(); old['chatMessage']['ts'] = 1
        assert dm.ingest(db, db.get(PlatformAccount, 1), db.get(DmListener, 1), old) is None


def test_exact_channel_payload_survives_lost_response_and_route_change(database):
    client = FakeChannel(); client.drop_response = True
    with database() as db:
        row = accept(db)
        with pytest.raises(ValueError): dm.poll_channel(db, row, client)
        db.get(DmListener, 1).route_id = 'changed-route'; db.commit()
        dm.poll_channel(db, row, client)
        assert client.calls[0] == client.calls[1]
        assert client.calls[1]['route_id'] == 'route-a'
        assert row.state == 'ready'
        assert row.reply_text == '合成测试回复'


def test_atomic_claim_receipt_and_no_resend(database):
    with database() as db:
        row = accept(db); dm.poll_channel(db, row, FakeChannel())
        sending = dm.claim_send(db, row.id)
        mid = sending.outbound_mid
        assert dm.claim_send(db, row.id) is None
        dm.finish_send(db, row.id, {'mid': mid, 'code': 0, 'msg': 'ok', 'messageid': 'remote-reply'})
        db.refresh(row)
        assert row.state == 'sent'
        assert row.receipt['messageid'] == 'remote-reply'
        assert row.receipt['msg'] == 'ok'
        assert dm.claim_send(db, row.id) is None


@pytest.mark.parametrize('ack', [None, {'mid': 'unrelated', 'code': 0}])
def test_missing_or_wrong_receipt_requires_reconciliation(database, ack):
    with database() as db:
        row = accept(db); dm.poll_channel(db, row, FakeChannel()); dm.claim_send(db, row.id)
        dm.finish_send(db, row.id, ack); db.refresh(row)
        assert row.state == 'uncertain'
        assert dm.claim_send(db, row.id) is None


def test_rejected_send_is_not_success(database):
    with database() as db:
        row = accept(db); dm.poll_channel(db, row, FakeChannel()); sending = dm.claim_send(db, row.id)
        dm.finish_send(db, row.id, {'mid': sending.outbound_mid, 'code': 3100001})
        db.refresh(row)
        assert row.state == 'failed'
        assert row.receipt['code'] == 3100001


@pytest.mark.parametrize('ack,state', [
    ({'code': 0, 'msg': 'message rejected'}, 'failed'),
    ({'code': 0}, 'uncertain'),
    ({'code': 0, 'msg': ''}, 'uncertain'),
    ({'code': False, 'msg': 'ok'}, 'uncertain'),
    ({'code': '0', 'msg': 'ok'}, 'uncertain'),
])
def test_ack_requires_explicit_platform_success_and_keeps_diagnostics(database, ack, state):
    from backend.app.models.direct_message import DmHistory
    with database() as db:
        row = accept(db); dm.poll_channel(db, row, FakeChannel()); sending = dm.claim_send(db, row.id)
        dm.finish_send(db, row.id, {**ack, 'mid': sending.outbound_mid, 'messageid': 'not-confirmed'})
        db.refresh(row)
        assert row.state == state
        assert row.receipt['msg'] == ack.get('msg')
        assert db.scalar(select(DmHistory).where(DmHistory.direction == 'outbound')) is None
        assert dm.claim_send(db, row.id) is None


@pytest.mark.parametrize('output', [
    {'text': 'analysis only'},
    {'text': 'raw', 'structured': {'reply_text': 'needs help', 'requires_human': True}},
    {'text': 'raw', 'structured': {'reply_text': 'ambiguous', 'requires_human': 'false'}},
])
def test_human_required_or_unstructured_outputs_are_held(database, output):
    with database() as db:
        client = FakeChannel(); client.output = output
        row = accept(db); dm.poll_channel(db, row, client)
        assert row.state == ('held' if output.get('structured', {}).get('requires_human') is True else 'failed')
        assert dm.claim_send(db, row.id) is None


@pytest.fixture
def api(database):
    app = FastAPI(); app.include_router(direct_messages.router, prefix='/api'); app.include_router(model_routing.router, prefix='/api')
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    def db():
        with database() as session: yield session
    app.dependency_overrides[get_db] = db
    return TestClient(app)


def test_listener_api_owner_isolation_and_resolution(api, database):
    assert api.get('/api/xhs/direct-messages?account_id=2').status_code == 404
    assert api.put('/api/xhs/direct-messages/listeners/2', json={'enabled': False}).status_code == 404
    with database() as db:
        row = accept(db); row.state = 'uncertain'; db.commit(); row_id = row.id
    assert api.post(f'/api/xhs/direct-messages/{row_id}/resolve', json={'outcome': 'confirmed_sent'}).status_code == 200
    assert api.get('/api/xhs/direct-messages?account_id=1').json()['items'][0]['state'] == 'sent'
    assert api.put('/api/xhs/direct-messages/listeners/1', json={'enabled': False}).json()['enabled'] is False


def test_routing_binding_uses_channel_not_keys(api, database, monkeypatch):
    route = {'id': 'route-a', 'name': 'Multica reply', 'agent': {'name': 'Agent'}}
    monkeypatch.setattr(model_routing, 'validate_route', lambda rid, purpose: route)
    assert api.put('/api/model-routing/text', json={'route_id': 'route-a'}).status_code == 200
    assert api.put('/api/model-routing/unknown', json={'route_id': 'route-a'}).status_code == 404
    with database() as db:
        config = db.scalar(select(ModelConfig).where(ModelConfig.user_id == 1))
        assert config.provider == 'multica-channel'
        assert config.model_name == 'route-a' and config.is_default
        assert config.encrypted_api_key == '' and config.base_url == ''


def test_text_generation_dispatches_to_multica(monkeypatch):
    from backend.app.services.ai_service import OpenAICompatibleTextClient
    from backend.app.services import channel_ai
    calls = []
    monkeypatch.setattr(channel_ai, 'complete', lambda config, text: calls.append((config, text)) or {'text': 'generated'})
    config = ModelConfig(provider='multica-channel', model_name='route-a', user_id=1)
    assert OpenAICompatibleTextClient().polish_text(model_config=config, api_key='', text='test', instruction='polish') == 'generated'
    assert calls[0][0] is config and 'test' in calls[0][1]


def test_pending_same_peer_is_serialized_and_other_peer_can_progress(database, monkeypatch):
    from backend.app.services import dm_worker
    monkeypatch.setattr(dm_worker, 'SessionLocal', database)
    with database() as db:
        row = accept(db); row.state = 'uncertain'; db.commit()
        listener, account = db.get(DmListener, 1), db.get(PlatformAccount, 1)
        for peer, mid in [('customer-a', 'later-same-peer'), ('customer-b', 'different-peer')]:
            incoming = event(sender=peer); incoming['chatMessage']['messageid'] = mid
            extra = dm.ingest(db, account, listener, incoming)
            dm.prepare_history(db, extra, empty_history)
            extra.state = 'ready'; extra.reply_text = 'reply'; db.commit()
            if peer == 'customer-b': other_id = extra.id
    assert dm_worker.next_delivery(1) == other_id
    dm_worker.recover()


def test_background_worker_full_synthetic_receive_channel_send_ack(database, monkeypatch):
    import asyncio
    from backend.app.services import dm_worker
    client = FakeChannel()
    monkeypatch.setattr(dm, 'channel_client', lambda: client)
    monkeypatch.setattr(dm_worker, 'SessionLocal', database)
    from backend.app.models import AccountCookieVersion
    from backend.app.core.security import encrypt_text
    with database() as db:
        db.add(AccountCookieVersion(platform_account_id=1, encrypted_cookies=encrypt_text('a1=synthetic; web_session=synthetic')))
        db.commit()
    sent = []
    class Connection:
        def __init__(self, *args): self.queue = asyncio.Queue()
        async def connect(self):
            await self.queue.put({'decoded': {'im': event()}})
        async def events(self):
            while True: yield await self.queue.get()
        async def heartbeat(self): pass
        def cookie_snapshot(self): return {'a1': 'synthetic', 'web_session': 'rotated'}
        history_page = staticmethod(empty_history)
        recent_peers = staticmethod(lambda: [])
        async def send(self, peer, text, mid):
            sent.append((peer, text, mid))
            await self.queue.put({'decoded': {'im': {'chatACK': {'mid': mid, 'code': 0, 'msg': 'ok', 'messageid': 'acknowledged'}}}})
        async def close(self): pass
    monkeypatch.setattr(dm_worker, 'XhsImConnection', Connection)
    async def exercise():
        task = asyncio.create_task(dm_worker.account_loop(1))
        try:
            for _ in range(100):
                with database() as db:
                    row = db.scalar(select(DirectMessage))
                    if row and row.state == 'sent': return
                await asyncio.sleep(0.05)
            pytest.fail('synthetic message did not reach acknowledged delivery')
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())
    assert len(sent) == 1 and sent[0][:2] == ('customer-a', '合成测试回复')
    assert client.calls[0]['conversation_id'] == 'customer-a'


def test_im_adapter_uses_current_signer_and_captured_device_header(monkeypatch):
    from backend.app.adapters.xhs import im_adapter
    calls = []
    class Auth:
        user_id = 'own'
        session_storage = {}
        local_storage = {}
        cookies = 'a1=synthetic; web_session=synthetic'
        sign_cookie = cookies
        dsl_pair = 'synthetic;pair'
        proxies = None
        profile = SimpleNamespace(session=SimpleNamespace(snapshot=lambda: {'XHS_TAB_DEVICE_ID':'tab', 'XHS_RWP_FINGERPRINT':'fingerprint'}))
        http_client = SimpleNamespace(get=lambda target, **kwargs: calls.append((target, kwargs)) or SimpleNamespace(
            cookies={}, json=lambda: {'success': True, 'data': {'aLt': 'synthetic-rwp-token', 'expiredTime': 60}}))
        def next_sign_context(self, api): return {'now': 1, 'tier': 'test'}
        def current_b1(self, ts): return ''
        def origin(self, kind): return 'https://edith.xiaohongshu.com'
        def cookies_for_url(self, target, cookies): return cookies
        def set_user_id(self, uid): self.user_id = uid
    auth = Auth()
    monkeypatch.setattr(im_adapter.XHSPcAuth, 'from_cookie', lambda cookies: auth)
    monkeypatch.setattr(im_adapter, 'XHS_Apis', lambda auth: SimpleNamespace(get_user_me=lambda: (
        True, '成功', {'data': {'user_id': 'own', 'nickname': '测试昵称'}})))
    monkeypatch.setattr(im_adapter, 'generate_request_params', lambda *args, **kwargs: ({'x-xray-traceid':'trace','x-t':'1','user-agent':'old','sec-ch-ua':'old'}, {}, ''))
    monkeypatch.setattr(im_adapter, 'build_pc_business_headers', lambda headers, *args, **kwargs: headers)
    connection = im_adapter.XhsImConnection('synthetic', 'own'); connection.bootstrap()
    headers = calls[0][1]['headers']
    assert list(headers).index('c_device_id') == list(headers).index('x-xray-traceid') + 1
    assert headers['c_device_id'] == 'tab' and 'sec-ch-ua' not in headers
    assert json.loads(auth.local_storage['RWP_LOGIN_TOKEN'])['uid'] == 'own'
    assert connection.nickname == '测试昵称'
    sent = []
    async def send_chat_message(**kwargs):
        sent.append(kwargs)
    connection.socket = SimpleNamespace(send_chat_message=send_chat_message)
    __import__('asyncio').run(connection.send('customer-a', '测试正文', 'stable-mid'))
    assert sent[0]['nickname'] == '测试昵称'
    assert sent[0]['sender'] == 'own' and sent[0]['receiver'] == 'customer-a'
    with pytest.raises(ValueError, match='不一致'):
        im_adapter.XhsImConnection('synthetic', 'other').bootstrap()


def test_real_source_sdk_preserves_explicit_route_and_catalog():
    from backend.app.services.channel_client import ChannelClient
    client = object.__new__(ChannelClient); client.source = 'xhs'
    calls = []
    client.request = lambda *args, **kwargs: calls.append((args, kwargs)) or {}
    client.catalog()
    client.send('own', 'stable', 'text', route_id='selected', surface='dm', event_type='dm.received')
    assert calls[0][0] == ('GET', '/v1/catalog')
    payload = calls[1][0][2]
    assert payload['route_id'] == 'selected' and payload['surface'] == 'dm'


def test_ai_unwraps_channel_reply_for_existing_content_features(monkeypatch):
    from backend.app.services import channel_ai
    client = FakeChannel()
    client.send = lambda *args, **kwargs: {'id':'channel-event'}
    client.status = lambda *args: {'state':'succeeded','result':{'output':{
        'text':'{"reply":"标题：合成标题\\n正文：合成正文"}', 'structured':{'reply':'标题：合成标题\n正文：合成正文'}}}}
    monkeypatch.setattr(channel_ai, 'channel_client', lambda: client)
    result = channel_ai.complete(ModelConfig(user_id=1,model_name='route'), 'synthetic')
    assert result['text'] == '标题：合成标题\n正文：合成正文'


def history_message(mid, store_id, at, text='合成历史', sender='customer-a', receiver='own', **kwargs):
    from backend.app.core.time import SHANGHAI_TZ
    return dict(id=mid, store_id=store_id, created_at=int(at.replace(tzinfo=SHANGHAI_TZ).timestamp()*1000),
        sender_id=sender, receiver_id=receiver, group_chat=False, revoked=False,
        content=json.dumps({'content': text, 'content_type': 1}), **kwargs)


def test_reconnect_recovers_only_new_inbound_text_after_enable_and_deduplicates(database, monkeypatch):
    from backend.app.services import dm_worker
    monkeypatch.setattr(dm_worker, 'SessionLocal', database)
    now = shanghai_now()
    messages = [history_message('newest', 5, now-timedelta(seconds=2), '新问题'),
        history_message('outbound', 4, now-timedelta(seconds=3), sender='own', receiver='customer-a'),
        history_message('first', 3, now-timedelta(seconds=4), '断线时第一条'),
        history_message('before-enable', 2, now-timedelta(minutes=2)),
        history_message('old-history', 1, now-timedelta(hours=49))]
    def fetch(peer, **kwargs):
        assert peer == 'customer-a'
        return {'result': {'success': True}, 'out_message_list': messages}
    assert dm_worker.reconnect_history(1, fetch, ['customer-a']) == 2
    assert dm_worker.reconnect_history(1, fetch, ['customer-a']) == 0
    with database() as db:
        rows = list(db.scalars(select(DirectMessage).order_by(DirectMessage.id)))
        assert [r.message_id for r in rows] == ['first', 'newest']
        assert all(r.receipt['recovered_after_reconnect'] for r in rows)
        assert all(r.state == 'history_pending' and r.outbound_mid == '' for r in rows)
        assert db.scalar(select(DirectMessage).where(DirectMessage.account_id == 2)) is None


def test_reconnect_does_not_replay_older_missing_history_or_revoked_nontext(database, monkeypatch):
    from backend.app.services import dm_worker, dm_history
    monkeypatch.setattr(dm_worker, 'SessionLocal', database)
    now = shanghai_now()
    with database() as db:
        row = accept(db, prepared=False)
        dm_history.save_message(db, 1, 'customer-a', row.message_id, 'inbound', row.text, now-timedelta(seconds=5))
        db.commit()
    revoked = history_message('revoked', 5, now-timedelta(seconds=2));revoked['revoked'] = True
    nontext = history_message('image', 4, now-timedelta(seconds=3));nontext['content'] = json.dumps({'content_type': 2})
    messages = [revoked, nontext, history_message('older-missing', 3, now-timedelta(seconds=10)),
        history_message('ancient', 1, now-timedelta(hours=49))]
    assert dm_worker.reconnect_history(1, lambda *a, **kw: {'result': {'success': True}, 'out_message_list': messages}) == 0
    with database() as db:
        assert len(list(db.scalars(select(DirectMessage)))) == 1
        db.get(DmListener, 1).enabled = False; db.commit()
    assert dm_worker.reconnect_history(1, lambda *a, **kw: pytest.fail('disabled listener must not fetch')) == 0


def test_listener_exposes_earliest_conversation_hold_and_clears_it_on_skip(database, api):
    with database() as db:
        row = accept(db);row.state = 'uncertain';row.error = '接收端未显示，待核对';db.commit();mid = row.id
    listener = api.get('/api/xhs/direct-messages/listeners').json()['items'][0]
    assert listener['conversation_holds'] == [{'message_id': mid, 'peer_id': 'customer-a', 'state': 'uncertain', 'reason': '接收端未显示，待核对'}]
    assert api.post(f'/api/xhs/direct-messages/{mid}/resolve', json={'outcome':'skip'}).status_code == 200
    assert api.get('/api/xhs/direct-messages/listeners').json()['items'][0]['conversation_holds'] == []


def test_paged_bidirectional_48h_history_is_persisted_and_scoped(database, api):
    from backend.app.services import dm_history
    from backend.app.models.direct_message import DmHistory
    now = shanghai_now()
    pages = {
        0: [history_message('new', 9, now-timedelta(minutes=1)),
            history_message('self', 8, now-timedelta(hours=1), '我的历史话术', sender='own', receiver='customer-a'),
            history_message('wrong', 7, now-timedelta(hours=2), sender='someone-else'),
            history_message('future', 6, now+timedelta(hours=1))],
        6: [history_message('self', 8, now-timedelta(hours=1), '我的历史话术', sender='own', receiver='customer-a'),
            history_message('older', 5, now-timedelta(hours=47)),
            history_message('expired', 4, now-timedelta(hours=49))],
    }
    calls = []
    def fetch(peer, last_id, limit):
        calls.append(last_id)
        assert peer == 'customer-a'
        return {'result': {'success': True}, 'out_message_list': pages[last_id]}
    with database() as db:
        status = dm_history.sync_history(db, db.get(PlatformAccount, 1), 'customer-a', fetch, now)
        assert status['complete'] and calls == [0, 6]
        rows = dm_history.history_rows(db, 1, 'customer-a', now-timedelta(hours=48), now)
        assert [r.message_id for r in rows] == ['older', 'self', 'new']
        assert rows[1].direction == 'outbound'
        assert db.scalars(select(DmHistory).where(DmHistory.account_id == 2)).all() == []
    response = api.get('/api/xhs/direct-messages/history?account_id=1&peer_id=customer-a').json()
    assert len(response['items']) == 3 and response['history']['complete']
    assert api.get('/api/xhs/direct-messages/history?account_id=2&peer_id=customer-a').status_code == 404


def test_history_snapshot_uses_full_sql_but_bounded_model_context(database):
    from backend.app.services import dm_history
    from backend.app.models.direct_message import DmHistory
    now = shanghai_now()
    with database() as db:
        row = accept(db, prepared=False)
        for i in range(40):
            dm_history.save_message(db, 1, 'customer-a', str(i), 'outbound' if i % 2 else 'inbound',
                '历史文字'*500, now-timedelta(minutes=50-i))
        db.commit()
        dm.prepare_history(db, row, empty_history)
        context = row.channel_payload['context']
        assert context['history']['total_messages'] == 41
        assert context['history']['included_messages'] < 41
        assert context['history']['context_truncated']
        assert len(json.dumps(context, ensure_ascii=False).encode()) < 22000
        assert len(db.scalars(select(DmHistory)).all()) == 41
        assert all(len(m['text']) <= 2000 for m in context['messages'])


def test_history_failure_does_not_submit_to_channel(database, monkeypatch):
    from backend.app.services import dm_worker
    monkeypatch.setattr(dm_worker, 'SessionLocal', database)
    client = FakeChannel(); monkeypatch.setattr(dm, 'channel_client', lambda: client)
    with database() as db:
        row_id = accept(db, prepared=False).id
    def broken(*args, **kwargs): raise ValueError('private network details')
    assert dm_worker.next_delivery(1, broken) is None
    with database() as db:
        row = db.get(DirectMessage, row_id)
        assert row.state == 'history_error'
        assert 'private' not in row.error
        assert row.channel_event_id == ''
    assert client.calls == []


def test_pagination_that_never_advances_is_not_used_as_complete(database):
    from backend.app.services import dm_history
    item = history_message('same', 9, shanghai_now()-timedelta(hours=1))
    with database() as db:
        with pytest.raises(dm_history.HistoryUnavailable, match='未向前'):
            dm_history.sync_history(db, db.get(PlatformAccount, 1), 'customer-a',
                lambda *a, **k: {'result': {'success': True}, 'out_message_list': [item, item]})


def test_no_reply_is_a_terminal_model_decision_and_analysis_is_saved(database):
    from backend.app.models.direct_message import DmConversation
    client = FakeChannel(); client.output = {'structured': {'decision': 'no_reply', 'reply_text': '',
        'requires_human': False, 'summary': '我们已经回复过，不重复接话', 'intent': '对方在结束话题', 'style': '简短随意'}}
    with database() as db:
        row = accept(db); dm.poll_channel(db, row, client)
        assert row.state == 'skipped' and row.reply_text == ''
        assert dm.claim_send(db, row.id) is None
        saved = db.scalar(select(DmConversation))
        assert saved.analysis['intent'] == '对方在结束话题'
        assert dm.message_view(row)['analysis']['summary'] == '我们已经回复过，不重复接话'


def test_handoff_shows_summary_and_clean_reply_instead_of_raw_json(database):
    client = FakeChannel(); client.output = {'text': '{raw json}', 'structured': {
        'reply_text': '合成拟回复', 'requires_human': True, 'summary': '需要确认本人尚未提供的行程安排'}}
    with database() as db:
        row = accept(db); dm.poll_channel(db, row, client)
        assert row.state == 'held' and row.reply_text == '合成拟回复'
        assert row.error == '需要确认本人尚未提供的行程安排'


def test_reanalysis_gets_new_id_and_retains_original_attempt(api, database):
    with database() as db:
        row = accept(db); dm.poll_channel(db, row, FakeChannel())
        original = json.loads(json.dumps(row.channel_payload))
        row.state = 'held'; db.commit(); row_id = row.id
    assert api.post(f'/api/xhs/direct-messages/{row_id}/reanalyze').status_code == 200
    with database() as db:
        row = db.get(DirectMessage, row_id)
        assert row.state == 'history_pending'
        assert row.channel_payload['message_id'] != original['message_id']
        assert row.receipt['analysis_attempts'][0]['payload'] == original
        assert 'analysis_attempts' not in dm.message_view(row)['receipt']
    assert api.post(f'/api/xhs/direct-messages/{row_id}/reanalyze').status_code == 409


def test_continuous_messages_are_coalesced_and_only_latest_can_send(database, monkeypatch):
    from backend.app.services import dm_worker
    monkeypatch.setattr(dm_worker, 'SessionLocal', database)
    client = FakeChannel(); monkeypatch.setattr(dm, 'channel_client', lambda: client)
    with database() as db:
        old = accept(db); old.state = 'held'; old.channel_payload = {**old.channel_payload, 'context': {'peer_id': old.peer_id}}
        db.commit(); old_id = old.id
        later = event('新的合成消息'); later['chatMessage']['messageid'] = 'latest'
        newest = dm.ingest(db, db.get(PlatformAccount, 1), db.get(DmListener, 1), later)
        newest_id = newest.id
    assert dm_worker.next_delivery(1, empty_history) == newest_id
    with database() as db:
        assert db.get(DirectMessage, old_id).state == 'superseded'
        assert db.get(DirectMessage, newest_id).state == 'ready'
    assert len(client.calls) == 1
    assert [m['text'] for m in client.calls[0]['context']['messages']] == ['合成测试咨询', '新的合成消息']


def test_new_inbound_between_selection_and_claim_prevents_stale_send(database):
    with database() as db:
        row = accept(db); dm.poll_channel(db, row, FakeChannel())
        later = event('更晚的消息'); later['chatMessage']['messageid'] = 'latest'
        dm.ingest(db, db.get(PlatformAccount, 1), db.get(DmListener, 1), later)
        assert dm.claim_send(db, row.id) is None


def test_acknowledged_outbound_is_saved_without_triggering_another_reply(database):
    from backend.app.models.direct_message import DmHistory
    with database() as db:
        row = accept(db); dm.poll_channel(db, row, FakeChannel()); sending = dm.claim_send(db, row.id)
        dm.finish_send(db, row.id, {'mid': sending.outbound_mid, 'code': 0, 'msg': 'ok', 'messageid': 'reply-id'})
        outgoing = db.scalar(select(DmHistory).where(DmHistory.direction == 'outbound'))
        assert outgoing.text == '合成测试回复'
        assert outgoing.account_id == 1 and outgoing.peer_id == 'customer-a'
        assert len(db.scalars(select(DirectMessage)).all()) == 1
        assert 'analysis' in row.receipt


def test_own_live_message_updates_sql_and_invalidates_an_unsent_reply(database):
    from backend.app.models.direct_message import DmHistory
    with database() as db:
        row = accept(db); dm.poll_channel(db, row, FakeChannel())
        dm.record_own_message(db, db.get(PlatformAccount, 1), event('我已手动回复', sender='own', receiver='customer-a'))
        assert dm.claim_send(db, row.id) is None
        outgoing = db.scalar(select(DmHistory).where(DmHistory.direction == 'outbound'))
        assert outgoing.text == '我已手动回复'
        assert len(db.scalars(select(DirectMessage)).all()) == 1


@pytest.mark.parametrize('mode,expected', [('request_approval', 'approval_pending'), ('auto_approve', 'ready'), ('full_access', 'ready')])
def test_three_control_modes_bind_to_model_snapshot_and_gate_delivery(database, api, mode, expected):
    with database() as db:
        listener = db.get(DmListener, 1); listener.control_mode = mode; db.commit()
        row = accept(db); dm.poll_channel(db, row, FakeChannel())
        assert row.state == expected
        assert row.channel_payload['context']['control_mode'] == mode
        assert mode not in row.text  # policy is separate from the original user's text
        row_id = row.id
        if mode == 'request_approval':
            assert dm.claim_send(db, row_id) is None
    if mode == 'request_approval':
        assert api.post(f'/api/xhs/direct-messages/{row_id}/approve').status_code == 200
        assert api.post(f'/api/xhs/direct-messages/{row_id}/approve').status_code == 409
        with database() as db:
            row = db.get(DirectMessage, row_id)
            assert row.receipt['approval']['approved_by'] == 1
            assert dm.claim_send(db, row_id).state == 'sending'


def test_full_access_handoff_is_a_contract_failure_not_a_no_reply_decision(database):
    client = FakeChannel(); client.output = {'structured': {'decision': 'handoff', 'reply_text': '',
        'requires_human': True, 'summary': '没有本人行程资料，无法确认安排'}}
    with database() as db:
        db.get(DmListener, 1).control_mode = 'full_access'; db.commit()
        row = accept(db); dm.poll_channel(db, row, client)
        assert row.state == 'failed'
        assert '模型违反完全访问模式' in row.error
        assert dm.claim_send(db, row.id) is None


def test_approval_does_not_survive_reanalysis_and_stale_approval_is_rejected(database, api):
    with database() as db:
        db.get(DmListener, 1).control_mode = 'request_approval'; db.commit()
        row = accept(db); dm.poll_channel(db, row, FakeChannel()); row_id = row.id
    assert api.post(f'/api/xhs/direct-messages/{row_id}/approve').status_code == 200
    assert api.post(f'/api/xhs/direct-messages/{row_id}/reanalyze').status_code == 200
    with database() as db:
        row = db.get(DirectMessage, row_id)
        assert 'approval' not in row.receipt
        dm.prepare_history(db, row, empty_history); dm.poll_channel(db, row, FakeChannel())
        later = event('更新的消息'); later['chatMessage']['messageid'] = 'later'
        dm.ingest(db, db.get(PlatformAccount, 1), db.get(DmListener, 1), later)
    assert api.post(f'/api/xhs/direct-messages/{row_id}/approve').status_code == 409


def test_control_mode_change_reanalyzes_pending_draft_under_new_policy(database, monkeypatch):
    from backend.app.services import dm_worker
    monkeypatch.setattr(dm_worker, 'SessionLocal', database)
    monkeypatch.setattr(dm, 'channel_client', FakeChannel)
    with database() as db:
        row = accept(db); dm.poll_channel(db, row, FakeChannel()); row_id = row.id
        db.get(DmListener, 1).control_mode = 'request_approval'; db.commit()
    assert dm_worker.next_delivery(1, empty_history) is None
    with database() as db:
        row = db.get(DirectMessage, row_id)
        assert row.state == 'approval_pending'
        assert row.channel_payload['context']['control_mode'] == 'request_approval'
        assert len(row.receipt['analysis_attempts']) == 1


def test_listener_control_mode_validation_and_owner_isolation(api, database, monkeypatch):
    monkeypatch.setattr(direct_messages, 'validate_route', lambda *args: {})
    data={'enabled': True, 'auto_reply': True, 'route_id': 'route-a', 'control_mode': 'full_access'}
    assert api.put('/api/xhs/direct-messages/listeners/1', json=data).json()['control_mode'] == 'full_access'
    data['control_mode'] = 'unsafe-arbitrary-mode'
    assert api.put('/api/xhs/direct-messages/listeners/1', json=data).status_code == 422
    with database() as db:
        row = DirectMessage(account_id=2,message_id='other-user',peer_id='peer',text='private',state='approval_pending',channel_payload={})
        db.add(row);db.commit();row_id=row.id
    assert api.post(f'/api/xhs/direct-messages/{row_id}/approve').status_code == 404
    assert api.post(f'/api/xhs/direct-messages/{row_id}/reanalyze').status_code == 404


def test_legacy_raw_json_draft_is_displayed_cleanly_without_rewriting_sql(database):
    with database() as db:
        row = accept(db)
        original=json.dumps({'reply_text':'好的呀～','summary':'缺少此前会话','requires_human':True},ensure_ascii=False)
        row.reply_text=original;row.state='held';db.commit()
        result=dm.message_view(row)
        assert result['reply_text']=='好的呀～' and result['error']=='缺少此前会话'
        db.refresh(row)
        assert row.reply_text==original

# Native media must survive intake, reconnect and the immutable model snapshot.
@pytest.mark.parametrize('kind,payload', [
    (2, {'link': 'https://msg-img.xhscdn.com/photo', 'size': {'width': 100, 'height': 100}}),
    (13, {'image': 'https://fe-platform.xhscdn.com/emoji', 'emojiKey': '[笑哭了R]'}),
    (16, {'url': 'https://msg-img.xhscdn.com/sticker', 'smileId': 'sticker-id', 'isGif': 1}),
])
def test_media_intake_history_and_vision(database, monkeypatch, tmp_path, kind, payload):
    from backend.app.services import dm_media
    from backend.app.models.direct_message import DmHistory, DmMedia
    from PIL import Image
    path = tmp_path / 'image.png'; Image.new('RGB', (80, 80), 'red').save(path)
    monkeypatch.setattr(dm_media, 'local_file', lambda db, asset: path)
    client = FakeChannel(); uploads = []
    client.upload = lambda account, blob, filename: uploads.append((account, blob)) or {'id': 'uploaded-image'}
    with database() as db:
        incoming = event(json.dumps({'content': json.dumps(payload)}), contentType=kind)
        row = dm.ingest(db, db.get(PlatformAccount, 1), db.get(DmListener, 1), incoming)
        assert row.media and row.text.startswith('[')
        assert dm.ingest(db, db.get(PlatformAccount, 1), db.get(DmListener, 1), incoming).id == row.id
        assert db.scalar(select(DmHistory)).content_type == kind
        dm.prepare_history(db, row, empty_history)
        client.drop_response = True
        with pytest.raises(ValueError): dm.poll_channel(db, row, client)
        durable = json.dumps(row.channel_payload, sort_keys=True)
        dm.poll_channel(db, row, client)
        assert json.dumps(row.channel_payload, sort_keys=True) == durable
        assert row.channel_payload['images'] == ['uploaded-image']
        assert len(uploads) == 1 and uploads[0][0] == 'user:1:account:own'
        assert row.channel_payload['context']['media_catalog'][0]['image_number'] == 1
        assert db.scalar(select(DmMedia)).account_id == 1


def test_media_reply_ownership_native_payload_and_receipt(database):
    from backend.app.services import dm_media
    from backend.app.models.direct_message import DmHistory
    with database() as db:
        incoming = event({'url': 'https://msg-img.xhscdn.com/sticker', 'smileId': 'id'}, contentType=16)
        row = dm.ingest(db, db.get(PlatformAccount, 1), db.get(DmListener, 1), incoming)
        dm.prepare_history(db, row, empty_history)
        key = row.media[0]['asset_id']
        row.channel_payload = {**row.channel_payload, 'context': {**row.channel_payload['context'],
            'media_prepared': True, 'media_catalog': [{**row.media[0], 'image_number': 1}]}}
        client = FakeChannel(); client.output = {'structured': {'decision': 'reply', 'requires_human': False,
            'reply_message': {'type': 'sticker', 'asset_id': key}}}
        dm.poll_channel(db, row, client)
        assert row.state == 'ready'
        assert json.loads(row.receipt['reply_message']['content'])['smileId'] == 'id'
        claimed = dm.claim_send(db, row.id)
        dm.finish_send(db, row.id, {'mid': claimed.outbound_mid, 'code': 0, 'msg': 'ok', 'messageid': 'media-ack'})
        outgoing = db.scalar(select(DmHistory).where(DmHistory.direction == 'outbound'))
        assert row.state == 'sent' and outgoing.media[0]['asset_id'] == key and outgoing.content_type == 16
        with pytest.raises(ValueError): dm_media.reply_message(db, row, {'reply_message': {'type': 'image', 'asset_id': 'other-account'}})


@pytest.mark.parametrize('url', ['http://127.0.0.1/x', 'https://192.168.1.1/x', 'file:///etc/passwd',
    'https://msg-img.xhscdn.com.evil.test/x', 'https://evil.test/x', 'https://a@msg-img.xhscdn.com/x', 'https://msg-img.xhscdn.com:8443/x'])
def test_media_download_rejects_untrusted_hosts(url):
    from backend.app.services.dm_media import safe_url, MediaUnavailable
    with pytest.raises(MediaUnavailable): safe_url(url)


def test_animation_vision_uses_spread_frames(tmp_path):
    from PIL import Image
    from backend.app.services.dm_media import vision_blob, validate_image
    path = tmp_path / 'animation.gif'
    frames = [Image.new('RGB', (40, 40), color) for color in ('red', 'green', 'blue')]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, loop=0)
    blob, count = vision_blob(path)
    assert count == 3 and validate_image(blob)[1] == 'image/jpeg'


def test_reconnect_recovers_media(database, monkeypatch):
    from backend.app.services import dm_worker
    monkeypatch.setattr(dm_worker, 'SessionLocal', database)
    raw = {'id': 'sticker-history', 'sender_id': 'customer-a', 'receiver_id': 'own', 'store_id': 1,
        'created_at': int(shanghai_now().replace(tzinfo=__import__('backend.app.core.time', fromlist=['SHANGHAI_TZ']).SHANGHAI_TZ).timestamp() * 1000),
        'content': json.dumps({'content_type': 16, 'content': json.dumps({'url': 'https://msg-img.xhscdn.com/sticker', 'smileId': 'id'})})}
    fetch = lambda peer, **kw: {'result': {'success': True}, 'out_message_list': [raw]}
    assert dm_worker.reconnect_history(1, fetch, ['customer-a']) == 1
    assert dm_worker.reconnect_history(1, fetch, ['customer-a']) == 0
    with database() as db:
        row = db.scalar(select(DirectMessage)); assert row.media and row.receipt['recovered_after_reconnect']


def test_private_conversation_media_preview_and_ownership(database, monkeypatch, tmp_path):
    from PIL import Image
    from backend.app.services import dm_media
    from backend.app.models.direct_message import DmMedia
    path = tmp_path / 'sample.png'
    Image.new('RGB', (12, 12), 'blue').save(path)
    monkeypatch.setattr(dm_media, 'ROOT', tmp_path)
    with database() as db:
        db.add(DmMedia(id='sample', account_id=1, kind='image', label='聊天图片',
            content_type=2, url='', native={}, filename=path.name, mime='image/png'))
        db.commit()
    app = FastAPI(); app.include_router(direct_messages.router)
    def db_session():
        with database() as db: yield db
    app.dependency_overrides[get_db] = db_session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    with TestClient(app) as client:
        preview = client.get('/xhs/direct-messages/media/sample', params={'account_id': 1})
        assert preview.status_code == 200 and preview.headers['content-type'] == 'image/png'
        assert preview.headers['cache-control'] == 'private, no-store'
        assert client.get('/xhs/direct-messages/media/sample', params={'account_id': 2}).status_code == 404
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2)
        assert client.get('/xhs/direct-messages/media/sample', params={'account_id': 1}).status_code == 404


def test_removed_media_library_endpoints_are_unavailable(api):
    for method, path in [('get', '/media-library'), ('post', '/media-library'),
                         ('delete', '/media-library/sample')]:
        assert getattr(api, method)('/api/xhs/direct-messages' + path).status_code == 404


def test_model_inputs_ignore_previously_uploaded_library_assets(database, monkeypatch):
    from backend.app.services import dm_media
    from backend.app.models.direct_message import DmMedia
    def forbidden(*args):
        raise AssertionError('uploaded library assets must not be read')
    monkeypatch.setattr(dm_media, 'local_file', forbidden)
    with database() as db:
        db.add(DmMedia(id='old-library', account_id=1, kind='sticker', label='旧素材',
            content_type=16, library=True, url='', native={}))
        db.commit()
        row = accept(db)
        dm_media.prepare_inputs(db, row, FakeChannel())
        assert row.channel_payload['context']['media_catalog'] == []
        assert not row.channel_payload.get('images')
        assert db.get(DmMedia, 'old-library') is not None


def test_distinct_pictures_close_in_time_are_not_merged(database):
    from backend.app.services import dm_history
    from backend.app.models.direct_message import DmHistory
    with database() as db:
        stamp = shanghai_now()
        def media(url): return [{'url': url, 'kind': 'image', 'label': '图片', 'content_type': 2, 'native': {'link': url}}]
        dm_history.save_message(db, 1, 'peer', 'live-a', 'inbound', '[图片]', stamp, content_type=2,
            source='live', media=media('https://msg-img.xhscdn.com/a'))
        dm_history.save_message(db, 1, 'peer', 'history-b', 'inbound', '[图片]', stamp, content_type=2,
            source='history', media=media('https://msg-img.xhscdn.com/b'))
        assert len(db.scalars(select(DmHistory)).all()) == 2


def burst_output(messages):
    client = FakeChannel()
    client.output = {'structured': {'decision': 'reply', 'requires_human': False,
        'reply_messages': messages, 'expression_memory': '对方喜欢轻松猫猫梗，不催回复'}}
    return client


def burst(db, count=3, delay=0):
    row = accept(db)
    dm.poll_channel(db, row, burst_output([{'type': 'text', 'text': f'消息{i}', 'delay_seconds': delay} for i in range(count)]))
    return row


def acknowledge(db, row):
    sending = dm.claim_send(db, row.id)
    assert sending is not None
    mid = sending.outbound_mid
    dm.finish_send(db, row.id, {'mid': mid, 'code': 0, 'msg': 'ok', 'messageid': 'remote-' + mid})
    return mid


def test_burst_is_ordered_durable_and_each_bubble_has_its_own_receipt(database):
    from backend.app.models.direct_message import DmHistory
    with database() as db:
        row = burst(db); rid = row.id
        assert row.receipt['analysis']['expression_memory'].startswith('对方喜欢')
        first = acknowledge(db, row)
        assert row.state == 'ready' and row.reply_text == '消息1'
    # A fresh SQL session can resume without resending the acknowledged bubble.
    with database() as db:
        row = db.get(DirectMessage, rid)
        second = acknowledge(db, row)
        third = acknowledge(db, row)
        assert len({first, second, third}) == 3 and row.state == 'sent'
        assert [x['status'] for x in row.receipt['reply_messages']] == ['sent'] * 3
        assert [x.text for x in db.scalars(select(DmHistory).where(DmHistory.direction == 'outbound').order_by(DmHistory.id))] == ['消息0', '消息1', '消息2']
        assert dm.claim_send(db, rid) is None


def test_burst_pacing_is_persisted_and_does_not_block_other_peers(database, monkeypatch):
    from backend.app.services import dm_worker
    monkeypatch.setattr(dm_worker, 'SessionLocal', database)
    with database() as db:
        row = burst(db, delay=5); rid = row.id
        assert dm.claim_send(db, rid) is None
        row.receipt = {**row.receipt, 'reply_due_at': (shanghai_now()-timedelta(seconds=1)).isoformat()}; db.commit()
        acknowledge(db, row)
        assert dm.claim_send(db, rid) is None
    assert dm_worker.next_delivery(1, empty_history) is None


@pytest.mark.parametrize('cause', ['inbound', 'manual', 'disabled', 'auto_off'])
def test_burst_stops_between_bubbles_on_new_activity_or_disabled_switch(database, monkeypatch, cause):
    from backend.app.services import dm_worker, dm_history
    monkeypatch.setattr(dm_worker, 'SessionLocal', database)
    monkeypatch.setattr(dm, 'channel_client', FakeChannel)
    with database() as db:
        row = burst(db); rid = row.id; acknowledge(db, row)
        if cause == 'inbound':
            incoming = event('等等，我还没说完'); incoming['chatMessage']['messageid'] = 'new-inbound'
            dm.ingest(db, db.get(PlatformAccount, 1), db.get(DmListener, 1), incoming)
        elif cause == 'manual':
            dm_history.save_message(db, 1, row.peer_id, 'manual', 'outbound', '主人接话', shanghai_now())
        elif cause == 'disabled': db.get(DmListener, 1).enabled = False
        else: db.get(DmListener, 1).auto_reply = False
        db.commit()
        assert dm.claim_send(db, rid) is None
    dm_worker.next_delivery(1, empty_history)
    with database() as db:
        row = db.get(DirectMessage, rid)
        assert row.receipt['reply_messages'][0]['status'] == 'sent'
        assert row.receipt['reply_messages'][1]['status'] == 'pending'
        if cause in ('manual', 'inbound'): assert row.state == 'superseded'


@pytest.mark.parametrize('ack', [None, {'code': 3100001, 'msg': 'rejected'}])
def test_burst_stops_after_failed_or_uncertain_ack(database, ack):
    with database() as db:
        row = burst(db); acknowledge(db, row)
        sending = dm.claim_send(db, row.id)
        dm.finish_send(db, row.id, {**ack, 'mid': sending.outbound_mid} if ack else None)
        assert row.state in ('uncertain', 'failed')
        assert dm.claim_send(db, row.id) is None
        assert row.receipt['reply_messages'][0]['status'] == 'sent'
        assert row.receipt['reply_messages'][2]['status'] == 'pending'


@pytest.mark.parametrize('messages', [[], [{}]*5, [{'type':'text','text':'x','delay_seconds':float('nan')}],
    [{'type':'text','text':'x','delay_seconds':True}], [{'type':'text','text':'x','delay_seconds':16}],
    [{'type':'text','text':'first'}, {'type':'image','asset_id':'invented'}]])
def test_burst_validates_all_bubbles_before_sending_any(database, messages):
    with database() as db:
        row = accept(db); dm.poll_channel(db, row, burst_output(messages))
        assert row.state == 'failed' and dm.claim_send(db, row.id) is None
        assert not row.receipt.get('reply_messages')


def test_burst_mixes_text_native_sticker_and_text(database):
    from backend.app.models.direct_message import DmMedia, DmHistory
    with database() as db:
        row = accept(db)
        db.add(DmMedia(id='cat',account_id=1,kind='sticker',label='抱抱',content_type=16,
            url='https://msg-img.xhscdn.com/cat',native={'url':'https://msg-img.xhscdn.com/cat','smileId':'cat'}));db.commit()
        row.channel_payload = {**row.channel_payload, 'context': {**row.channel_payload['context'],
            'media_prepared': True, 'media_catalog': [{'asset_id':'cat','image_number':1}]}};db.commit()
        dm.poll_channel(db, row, burst_output([{'type':'text','text':'今天辛苦了','delay_seconds':0},
            {'type':'sticker','asset_id':'cat','delay_seconds':0}, {'type':'kaomoji','text':'(｡･ω･｡)','delay_seconds':0}]))
        acknowledge(db,row)
        assert row.receipt['reply_message']['content_type'] == 16
        acknowledge(db,row);acknowledge(db,row)
        assert row.state == 'sent'
        assert [x.content_type for x in db.scalars(select(DmHistory).where(DmHistory.direction=='outbound').order_by(DmHistory.id))] == [1,16,1]


def test_partial_burst_cannot_be_reanalyzed_or_replayed(database, api):
    with database() as db:
        row = burst(db);rid=row.id;acknowledge(db,row)
    assert api.post(f'/api/xhs/direct-messages/{rid}/reanalyze').status_code == 409


def test_visual_inputs_prioritize_current_then_recent_conversation_images():
    from backend.app.services.dm_media import select_visual_assets
    row = SimpleNamespace(media=[{'asset_id': 'current'}])
    context = {'messages': [{'media': [{'asset_id': f'history-{i}'} for i in range(10)]},
                            {'media': [{'asset_id': 'current'}]}]}
    selected = select_visual_assets(row, context)
    assert selected == ['current'] + [f'history-{i}' for i in range(9, 2, -1)]


def test_burst_restart_retains_uncertainty_and_never_replays_next_bubble(database, monkeypatch):
    from backend.app.services import dm_worker
    monkeypatch.setattr(dm_worker, 'SessionLocal', database)
    with database() as db:
        row = burst(db); rid = row.id; acknowledge(db, row); dm.claim_send(db, rid)
    dm_worker.recover()
    with database() as db:
        row = db.get(DirectMessage, rid)
        assert row.state == 'uncertain'
        assert [x['status'] for x in row.receipt['reply_messages']] == ['sent','uncertain','pending']
        assert dm.claim_send(db, rid) is None


def test_control_mode_change_between_bubble_selection_and_claim_cannot_bypass_approval(database):
    with database() as db:
        row = burst(db); acknowledge(db,row)
        db.get(DmListener,1).control_mode = 'request_approval';db.commit()
        assert dm.claim_send(db,row.id) is None


def test_stale_dispatcher_cannot_overwrite_a_later_bubble(database):
    with database() as db:
        row=burst(db);rid=row.id
    with database() as stale, database() as active:
        old=stale.get(DirectMessage,rid)
        acknowledge(active,active.get(DirectMessage,rid))
        # A dispatcher holding the previous ready snapshot must not reclaim it.
        assert old.receipt['reply_index']==0
        assert dm.claim_send(stale,rid) is None
    with database() as db:
        row=db.get(DirectMessage,rid)
        assert row.receipt['reply_index']==1
        assert row.receipt['reply_messages'][0]['status']=='sent'
