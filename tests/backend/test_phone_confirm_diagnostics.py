"""Diagnose login stages without sending SMS or retaining authentication material."""
import json
from types import SimpleNamespace

import pytest

from apis.xhs_pc_login_apis import XHSLoginApi
from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
from backend.app.services.login_errors import login_failure_detail


@pytest.mark.parametrize('check,login,phase,code,posted', [
    ({'success': False, 'code': 1234, 'msg': 'private-phone private-code'}, None, 'checking_code', 1234, False),
    ({'success': True, 'code': 0, 'data': {}}, None, 'checking_code', 0, False),
    ({'success': True, 'code': 0, 'data': {'mobile_token': 'private-token'}},
     {'success': False, 'code': 5678, 'msg': 'private-session'}, 'submitting_login', 5678, True),
])
def test_sdk_preserves_phase_and_code_without_secrets(check, login, phase, code, posted, monkeypatch):
    monkeypatch.setattr('apis.xhs_pc_login_apis.build_pc_login_headers', lambda headers, *args, **kwargs: headers)
    sdk = XHSLoginApi.__new__(XHSLoginApi)
    sdk.base_url = 'https://example.test'
    sdk.proxies = None
    sdk._signed_request_params = lambda *args, **kwargs: ({}, '')
    sdk._cookies_for_url = lambda *args: {}
    sdk._merge_response_cookies = lambda *args: None
    calls = []
    def post(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(status_code=200, json=lambda: login)
    sdk.http = SimpleNamespace(get=lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: check), post=post)
    success, _, _ = sdk.login_by_phone('private-phone', 'private-code', {})
    assert not success
    diagnostics = sdk.last_phone_diagnostics
    assert diagnostics['phase'] == phase and diagnostics['platform_code'] == code
    assert bool(calls) is posted
    assert 'private-' not in json.dumps(diagnostics)


def test_adapter_carries_safe_platform_failure_to_ui_and_closes(monkeypatch):
    from apis import xhs_pc_login_apis as module
    closed = []
    class Api:
        last_phone_diagnostics = {'phase': 'submitting_login', 'code_check_passed': True, 'platform_code': 5678}
        def __init__(self, **kwargs): pass
        def login_by_phone(self, *args):
            return False, 'private-phone private-code private-token', {}
        def close(self): closed.append(True)
    monkeypatch.setattr(module, 'XHSLoginApi', Api)
    adapter = XhsPcLoginAdapter()
    with pytest.raises(RuntimeError) as failure:
        adapter.confirm_phone_login('private-phone', 'private-code', {})
    detail = login_failure_detail(failure.value, '验证码登录失败')
    assert '验证码已通过校验' in detail and '5678' in detail
    assert 'private-' not in detail and closed == [True]


def test_unknown_verification_response_is_not_called_incorrect_code():
    error = RuntimeError('private-payload')
    error.phase = 'checking_code'
    error.platform_code = 1234
    detail = login_failure_detail(error, '验证码登录失败')
    assert '校验未完成' in detail and '1234' in detail
    assert '不正确或已过期' not in detail and 'private-' not in detail


@pytest.mark.parametrize('identity_failure', [False, True, 'security'])
def test_failed_confirmation_is_durable_without_code_or_cookies(tmp_path, identity_failure):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from backend.app.api import login_sessions
    from backend.app.core.database import Base, get_db
    from backend.app.core.deps import get_current_user
    from backend.app.core.security import encrypt_text
    from backend.app.models import User, LoginSession, ApiLog
    engine = create_engine(f'sqlite:///{tmp_path / "login.db"}', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add(User(id=1, username='owner', password_hash='unused'))
        db.add(LoginSession(id=7, user_id=1, platform='xhs', sub_type='pc', login_method='phone',
                            encrypted_temp_cookies=encrypt_text('{"a1":"private-cookie"}')))
        db.commit()
    calls = []
    class Adapter:
        last_phone_diagnostics = {'phase': 'submitting_login', 'platform_code': 5678,
                                  'code_check_passed': True, 'cookies': 'private-cookie', 'phone': 'private-phone'}
        def confirm_phone_login(self, *args):
            calls.append('confirm')
            if identity_failure is True:
                return {'cookies': {'a1': 'private-cookie'}}
            error = RuntimeError('private-token')
            error.phase = 'submitting_login'; error.platform_code = 5678
            if identity_failure == 'security':
                error.http_status = 471
            raise error
        def get_user_info(self, cookies):
            raise RuntimeError('private-profile')
    def database():
        with sessions() as db:
            yield db
    app = FastAPI(); app.include_router(login_sessions.router)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[login_sessions.get_pc_login_adapter] = lambda: Adapter()
    response = TestClient(app).post('/xhs/login-sessions/pc/phone/confirm',
        json={'session_id': 7, 'phone': 'private-phone', 'code': '123456'})
    assert response.status_code == 400 and 'private-' not in response.text
    assert calls == ['confirm']
    with sessions() as db:
        log = db.scalar(select(ApiLog))
        assert log.status == 'failed' and log.meta['session_id'] == 7
        assert log.meta['phase'] == ('checking_identity' if identity_failure is True else 'submitting_login')
        assert 'private-' not in json.dumps(log.meta) and '123456' not in json.dumps(log.meta)
        assert db.get(LoginSession, 7).status == ('verification_required' if identity_failure == 'security' else 'pending')
    if identity_failure == 'security':
        retry = TestClient(app).post('/xhs/login-sessions/pc/phone/confirm',
            json={'session_id': 7, 'phone': 'private-phone', 'code': '123456'})
        assert retry.status_code == 409 and 'HTTP 471' in retry.text
        assert calls == ['confirm']
    engine.dispose()


@pytest.mark.parametrize('phase', ['checking_code', 'submitting_login'])
def test_http_471_overrides_misleading_success_envelope(phase, monkeypatch):
    from apis import xhs_pc_login_apis as module
    monkeypatch.setattr(module, 'build_pc_login_headers', lambda headers, *args, **kwargs: headers)
    sdk = XHSLoginApi.__new__(XHSLoginApi)
    sdk.base_url = 'https://example.test'
    sdk.proxies = None
    sdk._signed_request_params = lambda *args, **kwargs: ({}, '')
    sdk._cookies_for_url = lambda *args: {}
    sdk._merge_response_cookies = lambda *args: None
    calls = []
    def response(status):
        return SimpleNamespace(status_code=status, json=lambda: {'success': True, 'code': 0,
                               'data': {'mobile_token': 'private-token', 'session': 'private-session'}})
    def post(*args, **kwargs):
        calls.append('post')
        return response(471)
    sdk.http = SimpleNamespace(get=lambda *args, **kwargs: response(471 if phase == 'checking_code' else 200), post=post)
    success, message, payload = sdk.login_by_phone('private-phone', '123456', {})
    assert success is False and 'HTTP 471' in message
    assert sdk.last_phone_diagnostics['phase'] == phase
    assert sdk.last_phone_diagnostics['http_status'] == 471
    assert 'web_session' not in payload['cookies']
    assert bool(calls) is (phase == 'submitting_login')
    assert 'private-' not in json.dumps(sdk.last_phone_diagnostics)
    error = RuntimeError(message)
    error.http_status = 471
    detail = login_failure_detail(error, '验证码登录失败')
    assert '额外安全验证' in detail and '请勿重复提交' in detail
