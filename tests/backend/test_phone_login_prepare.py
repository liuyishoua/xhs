import time
from types import SimpleNamespace
import pytest
from backend.app.services import phone_login_prepare as store

@pytest.fixture(autouse=True)
def empty_store():
    with store._lock:
        for token in list(store._entries):store._discard(token)
    yield
    with store._lock:
        for token in list(store._entries):store._discard(token)

class Api:
    def __init__(self):self.closed=False
    def close(self):self.closed=True


def test_owner_scoped_reusable_prepare_is_consumed_once():
    api=Api();calls=[]
    def factory():calls.append('init');return api,{'gid':'ready'},{'initialization_ms':12000}
    token,_=store.prepare(1,2,factory)
    assert store.prepare(1,2,factory)[0]==token
    assert calls==['init']
    with pytest.raises(ValueError):store.take(token,99,2)
    with pytest.raises(ValueError):store.take(token,1,3)
    assert store.take(token,1,2).api is api
    with pytest.raises(ValueError):store.take(token,1,2)
    api.close()


def test_expired_context_is_closed_and_never_used():
    api=Api();token,_=store.prepare(1,None,lambda:(api,{},{}))
    store._entries[token].expires_at=time.monotonic()-1
    with pytest.raises(ValueError):store.take(token,1,None)
    assert api.closed


def test_capacity_and_failed_prepare_release_slot():
    for i in range(store.MAX_ENTRIES):store.prepare(i,None,lambda:(Api(),{},{}))
    with pytest.raises(ValueError):store.prepare(999,None,lambda:(Api(),{},{}))
    with store._lock:store._discard(next(iter(store._entries)))
    def fail():raise OSError('network')
    with pytest.raises(OSError):store.prepare(999,None,fail)
    store.prepare(999,None,lambda:(Api(),{},{}))


def test_send_reuses_same_profile_without_bootstrap():
    from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
    class Ready(Api):
        def export_phone_state(self):return {'synthetic': 'state'}
        def send_phone_code(self,phone,cookies):
            assert cookies=={'gid':'ready'}
            return True,'ok',{'code':0}
        def generate_init_cookies(self):raise AssertionError('repeated initialization')
    api=Ready();prepared=SimpleNamespace(api=api,cookies={'gid':'ready'},timings={'initialization_ms':12000})
    result=XhsPcLoginAdapter().send_prepared_phone_code('synthetic',prepared)
    assert result['cookies']['gid']=='ready'
    assert result['pc_login_state']=={'synthetic':'state'}
    assert api.closed
