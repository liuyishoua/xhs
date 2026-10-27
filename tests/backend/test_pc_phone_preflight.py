from types import SimpleNamespace
import pytest
from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
from apis import xhs_pc_login_apis as sdk

@pytest.mark.parametrize('accepted', [True, 'false'])
def test_phone_send_reports_webprofile_before_sms_and_closes(monkeypatch, accepted):
    calls = []
    class Api:
        def __init__(self, **kwargs): pass
        def generate_init_cookies(self): calls.append('initialize');return {'a1':'fake'}
        def ensure_webprofile(self, cookies): calls.append('webprofile');cookies['gid']='fake-gid'
        def send_phone_code(self, phone, cookies):
            assert cookies['gid']=='fake-gid'
            calls.append('sms');return accepted,'platform message',{'code':0,'private_phone':'never-log','token':'never-log'}
        def export_phone_state(self):return {'synthetic': 'state'}
        def close(self):calls.append('close')
    monkeypatch.setattr(sdk,'XHSLoginApi',Api)
    adapter=XhsPcLoginAdapter()
    if accepted is True:adapter.create_phone_session('fake-phone')
    else:
        with pytest.raises(RuntimeError):adapter.create_phone_session('fake-phone')
    assert calls==['initialize','webprofile','sms','close']
    assert adapter.last_phone_diagnostics['platform_success'] is (accepted is True)
    assert 'never-log' not in str(adapter.last_phone_diagnostics)
    assert 'total_ms' in adapter.last_phone_diagnostics

def test_failed_webprofile_never_sends_code(monkeypatch):
    calls=[]
    class Api:
        def __init__(self, **kwargs):pass
        def generate_init_cookies(self):return {}
        def ensure_webprofile(self, cookies):raise TimeoutError('profile failed')
        def send_phone_code(self,*args):calls.append('sms')
        def export_phone_state(self):return {'synthetic': 'state'}
        def close(self):calls.append('close')
    monkeypatch.setattr(sdk,'XHSLoginApi',Api)
    adapter=XhsPcLoginAdapter()
    with pytest.raises(TimeoutError):adapter.create_phone_session('fake-phone')
    assert calls==['close']
    assert adapter.last_phone_diagnostics['phase']=='initializing_login'
