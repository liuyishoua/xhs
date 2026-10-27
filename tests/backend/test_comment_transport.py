"""Comment receipt diagnostics must describe the actual request without secrets."""
from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest

from backend.app.adapters.xhs import pc_api_adapter as mod


@pytest.mark.parametrize('outcome', ['accepted', 'rejected', 'lost_ack'])
def test_comment_transport_records_actual_post_and_closes_client(monkeypatch, outcome):
    calls = []
    closed = []
    body = {'success': outcome == 'accepted', 'code': 0 if outcome == 'accepted' else -1,
            'data': {'comment': {'id': 'reply-id', 'status': 2}}}

    def post(url, **options):
        calls.append((url, json.loads(options['data'].decode('utf-8'))))
        if outcome == 'lost_ack':
            raise TimeoutError('synthetic timeout')
        return SimpleNamespace(status_code=200, headers={'xhs-real-ip': '2001:db8::7'}, json=lambda: body)

    api = SimpleNamespace(base_url='https://edith.xiaohongshu.com',
        http=SimpleNamespace(post=post, close=lambda: closed.append(True)),
        _request_params=lambda path, data, method: ({'x-s': 'synthetic-secret'}, {'session': 'synthetic-cookie'}, json.dumps(data, ensure_ascii=False)))
    adapter = mod.XhsPcApiAdapter('synthetic')
    monkeypatch.setattr(adapter, '_api', lambda: api)
    monkeypatch.setattr(mod, 'direct_xhs_request_env', nullcontext)
    if outcome == 'accepted':
        result = adapter.post_comment('note-id', '谢谢😊', 'parent-id')
        assert result['_transport'] == {'client': 'pc_web_direct', 'endpoint': '/api/sns/web/v1/comment/post',
            'http_status': 200, 'platform_observed_exit_ip': '2001:db8::7'}
        assert result['data']['comment']['status'] == 2
        assert 'synthetic-secret' not in json.dumps(result)
        assert 'synthetic-cookie' not in json.dumps(result)
    else:
        with pytest.raises(TimeoutError if outcome == 'lost_ack' else RuntimeError):
            adapter.post_comment('note-id', '谢谢😊', 'parent-id')
    assert calls == [('https://edith.xiaohongshu.com/api/sns/web/v1/comment/post',
                      {'note_id': 'note-id', 'content': '谢谢😊', 'at_users': [], 'target_comment_id': 'parent-id'})]
    assert closed == [True]


@pytest.mark.parametrize('guest', [False, True])
def test_profile_refresh_exposes_cookie_changes_only_for_authenticated_profile_and_closes_client(monkeypatch, guest):
    closed = []
    api = SimpleNamespace(auth=SimpleNamespace(profile=SimpleNamespace(cookie_map={'web_session': 'rotated'})),
        get_user_me=lambda: (True, '', {'data': {'user_id': 'own', 'guest': guest}}),
        http=SimpleNamespace(close=lambda: closed.append(True)))
    adapter = mod.XhsPcApiAdapter('web_session=old')
    monkeypatch.setattr(adapter, '_api', lambda: api)
    monkeypatch.setattr(mod, 'direct_xhs_request_env', nullcontext)
    assert adapter.get_self_info()['guest'] is guest
    assert adapter.refreshed_cookies == (None if guest else {'web_session': 'rotated'})
    assert closed == [True]
