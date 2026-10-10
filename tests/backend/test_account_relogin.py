"""Explicit re-login must preserve the selected account and reject another identity."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.api import accounts, login_sessions
from backend.app.core.database import Base, get_db
from backend.app.core.deps import get_current_user
from backend.app.core.security import decrypt_text, encrypt_text
from backend.app.main import app
from backend.app.models import AccountCookieVersion, LoginSession, PlatformAccount, User
from backend.app.models.direct_message import DmListener
from backend.app.services.account_service import decode_cookie_text


class LoginAdapter:
    identity = 'own'

    def create_qrcode(self):
        return {'qr_id': 'qr', 'code': 'code', 'qr_url': 'https://example.test/qr', 'cookies': {'a1': 'temp'}}

    def check_qrcode_status(self, *args):
        return {'status': 'confirmed', 'cookies': {'a1': 'device', 'web_session': 'new'}}

    def create_phone_session(self, phone):
        return {'cookies': {'a1': 'temp'}}

    def confirm_phone_login(self, *args):
        return {'cookies': {'a1': 'device', 'web_session': 'new'}}

    def get_user_info(self, cookies):
        return {'external_user_id': self.identity, 'nickname': 'renewed'}


@pytest.fixture
def relogin(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'relogin.db'), connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(User(id=1, username='owner', password_hash='test'))
        db.add(User(id=2, username='other', password_hash='test'))
        db.add(PlatformAccount(id=1, user_id=1, platform='xhs', sub_type='pc', external_user_id='own', status='expired'))
        db.add(PlatformAccount(id=2, user_id=2, platform='xhs', sub_type='pc', external_user_id='other'))
        db.add(AccountCookieVersion(platform_account_id=1, encrypted_cookies=encrypt_text('a1=device; web_session=old')))
        db.add(DmListener(account_id=1, enabled=True, auto_reply=False, route_id='saved-route'))
        db.commit()
    def database():
        with factory() as db:
            yield db
    adapter = LoginAdapter()
    previous = app.dependency_overrides.copy()
    app.dependency_overrides.update({
        get_db: database,
        get_current_user: lambda: SimpleNamespace(id=1),
        accounts.get_pc_account_adapter: lambda: adapter,
        accounts.get_creator_account_adapter: lambda: adapter,
        accounts.get_xhs_self_profile_adapter: lambda: SimpleNamespace(get_self_profile=lambda cookies: {}),
        login_sessions.get_pc_login_adapter: lambda: adapter,
        login_sessions.get_creator_login_adapter: lambda: adapter,
    })
    monkeypatch.setattr(login_sessions.XhsPcApiAdapter, 'get_self_info', lambda self: {})
    yield TestClient(app), factory, adapter
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)
    engine.dispose()


def complete_login(client, method, sub_type, account_id=1):
    if method == 'cookie':
        return client.post('/api/accounts/import-cookie', json={
            'platform': 'xhs', 'sub_type': sub_type, 'account_id': account_id,
            'cookie_string': 'a1=device; web_session=new'})
    path = f'/api/xhs/login-sessions/{sub_type}'
    if method == 'qr':
        response = client.post(path + '/qrcode', json={'account_id': account_id})
        if response.status_code != 200:
            return response
        return client.get('/api/xhs/login-sessions/' + str(response.json()['session_id']))
    response = client.post(path + '/phone/send-code', json={'phone': '13800138000', 'account_id': account_id})
    if response.status_code != 200:
        return response
    return client.post(path + '/phone/confirm', json={
        'session_id': response.json()['session_id'], 'phone': '13800138000', 'code': '123456'})


@pytest.mark.parametrize('method', ['qr', 'phone', 'cookie'])
@pytest.mark.parametrize('sub_type', ['pc', 'creator'])
@pytest.mark.parametrize('mismatch', [False, True])
def test_relogin_preserves_account_and_listener_or_rejects_wrong_identity(relogin, method, sub_type, mismatch):
    client, factory, adapter = relogin
    with factory() as db:
        db.get(PlatformAccount, 1).sub_type = sub_type
        db.commit()
    adapter.identity = 'wrong' if mismatch else 'own'
    response = complete_login(client, method, sub_type)
    assert response.status_code == (409 if mismatch else 200), response.text
    if not mismatch:
        payload = response.json() if method == 'cookie' else response.json()['account']
        assert payload['id'] == 1 and payload['action'] == 'updated' and payload['status'] == 'active'
    with factory() as db:
        account = db.get(PlatformAccount, 1)
        assert account.external_user_id == 'own'
        assert account.status == ('expired' if mismatch else 'active')
        assert len(db.scalars(select(PlatformAccount)).all()) == 2
        versions = db.scalars(select(AccountCookieVersion).order_by(AccountCookieVersion.id)).all()
        assert len(versions) == (1 if mismatch else 2)
        assert decode_cookie_text(decrypt_text(versions[-1].encrypted_cookies))['web_session'] == ('old' if mismatch else 'new')
        listener = db.get(DmListener, 1)
        assert listener.enabled and listener.route_id == 'saved-route' and not listener.auto_reply
        if mismatch and method != 'cookie':
            assert db.scalars(select(LoginSession)).one().status == 'pending'


@pytest.mark.parametrize('method', ['qr', 'phone', 'cookie'])
@pytest.mark.parametrize('account_id,sub_type,expected', [(2, 'pc', 404), (999, 'pc', 404), (1, 'creator', 400)])
def test_relogin_checks_ownership_existence_and_account_type_before_login(relogin, method, account_id, sub_type, expected):
    client, factory, adapter = relogin
    def forbidden(*args):
        raise AssertionError('must reject target before contacting XHS')
    adapter.create_qrcode = adapter.create_phone_session = adapter.get_user_info = forbidden
    assert complete_login(client, method, sub_type, account_id).status_code == expected
