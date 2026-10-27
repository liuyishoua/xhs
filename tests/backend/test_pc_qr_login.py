"""QR scan acknowledgement must not be confused with a usable login session."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.xhs_pc_login_apis import XHSLoginApi, QrCodeState
from backend.app.api import login_sessions
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.security import encrypt_text
from xhs_utils.xhs_pc.params import get_request_headers_template


def client_for_response(status, data):
    client = XHSLoginApi.__new__(XHSLoginApi)
    client.base_url = 'https://example.test'
    client.proxies = None
    client.profile = SimpleNamespace(update_cookies=lambda cookies: None)
    client._signed_request_params = lambda *args, **kwargs: (get_request_headers_template(method='GET'), '')
    client._cookies_for_url = lambda url, cookies: cookies
    client._merge_response_cookies = lambda *args, **kwargs: None
    response = SimpleNamespace(ok=200 <= status < 300, status_code=status,
                               json=lambda: {'success': True, 'msg': '成功', 'data': data})
    client.http = SimpleNamespace(get=lambda *args, **kwargs: response)
    return client


def test_http_rejection_with_success_body_never_creates_authenticated_session():
    api = client_for_response(471, {})
    cookies = {'web_session': 'visitor'}
    with pytest.raises(RuntimeError, match='HTTP 471'):
        api._login_by_qrcode_status('qr', 'code', cookies)
    assert cookies == {'web_session': 'visitor'}


@pytest.mark.parametrize('state,message', [(0, '扫描'), (1, '确认'), (3, '过期')])
def test_final_endpoint_interactive_states_are_not_reported_as_success(state, message):
    api = client_for_response(200, {'code_status': state})
    with pytest.raises(QrCodeState, match=message):
        api._login_by_qrcode_status('qr', 'code', {'web_session': 'visitor'})


def test_authenticated_session_replaces_visitor_only_after_final_confirmation():
    api = client_for_response(200, {'code_status': 2, 'login_info': {'session': 'authenticated'}})
    cookies = {'web_session': 'visitor'}
    assert api._login_by_qrcode_status('qr', 'code', cookies)['web_session'] == 'authenticated'


def test_final_confirmation_without_session_cannot_use_visitor_credentials():
    api = client_for_response(200, {'code_status': 2})
    with pytest.raises(RuntimeError, match='正式 web_session'):
        api._login_by_qrcode_status('qr', 'code', {'web_session': 'visitor'})


def test_poll_reports_actionable_verification_failure_and_preserves_pending_state():
    session = SimpleNamespace(id=12, user_id=1, status='pending', sub_type='pc', qr_id='qr', code='code',
                              encrypted_temp_cookies=encrypt_text('{"a1":"synthetic"}'))
    class Adapter:
        def check_qrcode_status(self, *args):
            raise RuntimeError('二维码登录请求被拒绝 (HTTP 471) private-cookie')
    app = FastAPI()
    app.include_router(login_sessions.router)
    app.dependency_overrides[get_db] = lambda: SimpleNamespace(get=lambda *args: session)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[login_sessions.get_pc_login_adapter] = Adapter
    response = TestClient(app).get('/xhs/login-sessions/12')
    assert response.status_code == 502
    assert 'HTTP 471' in response.json()['detail'] and '官方网页' in response.json()['detail']
    assert 'private-cookie' not in response.text
    assert session.status == 'pending'


def test_pc_adapter_closes_transport_and_keeps_successful_confirmation(monkeypatch):
    from apis import xhs_pc_login_apis
    from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
    from backend.app.services import xhs_network
    closed = []
    class Api:
        def __init__(self, **kwargs): pass
        def check_qrcode_status(self, *args): return True, 'confirmed', {'web_session': 'authenticated'}
        def close(self): closed.append(True)
    monkeypatch.setattr(xhs_network, 'http_options', lambda: {})
    monkeypatch.setattr(xhs_pc_login_apis, 'XHSLoginApi', Api)
    result = XhsPcLoginAdapter().check_qrcode_status('qr', 'code', {})
    assert result['status'] == 'confirmed' and closed == [True]
