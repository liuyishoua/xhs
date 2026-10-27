import json
from dataclasses import asdict

from apis.xhs_pc_login_apis import XHSLoginApi
from backend.app.api.login_sessions import _dump_temp_state, _load_temp_state
from backend.app.core.security import encrypt_text, decrypt_text


class Http:
    def close(self):
        pass


def test_encrypted_phone_state_survives_new_client_without_changing_cookie_scope():
    cookies = {'a1': 'synthetic-a1', 'gid': 'synthetic-gid', 'loadts': '1700000000000'}
    first = XHSLoginApi(http_client=Http(), dsl='synthetic-dsl')
    profile = first._ensure_profile(cookies)
    profile.session.mns_seq = 23
    profile.session.last_tiga_update_time = 1700000000001
    profile.session.sign_count = 17
    first._login_b1 = 'synthetic-login-b1'
    first._webprofile_reported = True
    first.profile_data = 'synthetic-profile-data'
    first._cookie_store.cookies_for_url(first.base_url, cookies)
    first._cookie_store.update(first.base_url, {'acw_tc': 'api-only'})
    first._cookie_store.update(first.as_url, {'acw_tc': 'security-only'})
    state = first.export_phone_state()
    encrypted = encrypt_text(_dump_temp_state(cookies, pc_login_state=state))
    assert 'api-only' not in encrypted
    decoded = decrypt_text(encrypted)
    restored_cookies, sync, account_id = _load_temp_state(decoded)
    second = XHSLoginApi(http_client=Http())
    second.restore_phone_state(restored_cookies, json.loads(decoded)['pc_login_state'])
    assert asdict(second.profile.session) == asdict(first.profile.session)
    assert asdict(second.profile.b1_state) == asdict(first.profile.b1_state)
    assert second._login_b1 == first._login_b1
    assert second._webprofile_reported is True
    assert second.dsl == first.dsl
    assert second.profile_data == first.profile_data
    assert second.host_cookie_state() == first.host_cookie_state()
    assert second._cookies_for_url(second.base_url, cookies)['acw_tc'] == 'api-only'
    assert second._cookies_for_url(second.as_url, cookies)['acw_tc'] == 'security-only'
    assert 'acw_tc' not in second._cookies_for_url(second.home_url, cookies)
    assert second.profile.session.next_seq() == 24
    assert (sync, account_id) == (False, None)


def test_confirm_restores_before_submitting_code_and_closes(monkeypatch):
    from apis import xhs_pc_login_apis as sdk
    from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
    calls = []
    class Api:
        def __init__(self, **kwargs):
            self.restored = False
        def restore_phone_state(self, cookies, state):
            assert state == {'private': 'synthetic'}
            self.restored = True
            calls.append('restore')
        def login_by_phone(self, phone, code, cookies):
            assert self.restored
            calls.append('confirm')
            return True, 'ok', {'cookies': cookies}
        def close(self):
            calls.append('close')
    monkeypatch.setattr(sdk, 'XHSLoginApi', Api)
    XhsPcLoginAdapter().confirm_phone_login('synthetic', '123456', {},
                                          pc_login_state={'private': 'synthetic'})
    assert calls == ['restore', 'confirm', 'close']
