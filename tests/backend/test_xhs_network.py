"""Transport and outage coverage; all network peers are local or synthetic."""
import asyncio
from types import SimpleNamespace
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.app.core.deps import get_current_user
from backend.app.services import xhs_network as network


@pytest.fixture
def home(monkeypatch):
    config = SimpleNamespace(xhs_network_mode='home', xhs_proxy_url='socks5://127.0.0.1:18890',
        xhs_network_label='家庭网络 · Mac mini', xhs_network_authorization_expires_at='2026-10-11T04:40:23Z')
    monkeypatch.setattr(network, 'get_settings', lambda: config)
    monkeypatch.setattr(network, '_snapshot', {})
    monkeypatch.setattr(network, '_checked_monotonic', 0)
    monkeypatch.setattr(network, '_configuration', None)
    return config


@pytest.mark.parametrize('url', ['', 'http://127.0.0.1:18890', 'socks5://127.0.0.1', 'socks5://127.0.0.1:bad', 'socks5://127.0.0.1:18890/path'])
def test_invalid_home_configuration_fails_closed(home, url):
    home.xhs_proxy_url = url
    with pytest.raises(network.HomeNetworkUnavailable):
        network.http_options()
    with pytest.raises(network.HomeNetworkUnavailable):
        network.require_ready()
    assert network.status()['state'] == 'unconfigured'


def test_real_outbound_probe_failure_never_retries_without_proxy(home, monkeypatch):
    from curl_cffi import requests
    calls = []
    class Client:
        def __init__(self, **options): calls.append(options)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def head(self, url, **options):
            assert options['timeout'] == 8
            raise RuntimeError('private-proxy-credential-must-not-leak')
    monkeypatch.setattr(requests, 'Session', Client)
    assert network.check()['state'] == 'offline'
    assert len(calls) == 1
    assert calls[0]['proxies'] == {'http': 'socks5h://127.0.0.1:18890', 'https': 'socks5h://127.0.0.1:18890'}
    assert 'private-proxy' not in str(network.status())
    with pytest.raises(network.HomeNetworkUnavailable): network.require_ready()


def test_health_recovery_platform_restriction_and_stale_observations(home, monkeypatch):
    from curl_cffi import requests
    response = SimpleNamespace(status_code=404, headers={'xhs-real-ip': '2001:db8::1'})
    class Client:
        def __init__(self, **options): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def head(self, *args, **options): return response
    monkeypatch.setattr(requests, 'Session', Client)
    assert network.check()['state'] == 'online'
    network.require_ready()
    assert network.status()['exit_ip'] == '2001:db8::1'
    assert '12:40:23+08:00' in network.status()['authorization_expires_at']
    monkeypatch.setattr(network, '_checked_monotonic', time.monotonic() - 46)
    with pytest.raises(network.HomeNetworkUnavailable): network.require_ready()
    response.status_code = 461
    assert network.check()['state'] == 'restricted'
    assert network.status()['exit_ip'] is None
    response.status_code = 200
    assert network.check()['state'] == 'online'


def test_pc_login_profile_and_im_bootstrap_share_the_home_exit(home, monkeypatch):
    from xhs_utils.xhs_pc import XHSPcAuth
    from backend.app.adapters.xhs.im_adapter import XhsImConnection
    from backend.app.adapters.xhs.pc_api_adapter import XhsPcApiAdapter
    from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
    from apis import xhs_pc_login_apis, xhs_pc_apis
    calls = []
    def auth(cookies, **options):
        calls.append(options)
        return SimpleNamespace(user_id='own')
    monkeypatch.setattr(XHSPcAuth, 'from_cookie', auth)
    monkeypatch.setattr(xhs_pc_apis, 'XHS_Apis', lambda auth: auth)
    XhsImConnection('synthetic', 'own')
    XhsPcApiAdapter('synthetic')._api()
    class Login:
        def __init__(self, **options): calls.append(options)
        def get_user_info(self, cookies): return True, {'user_id': 'own'}, None
        def close(self): pass
    monkeypatch.setattr(xhs_pc_login_apis, 'XHSLoginApi', Login)
    assert XhsPcLoginAdapter().get_user_info({})['external_user_id'] == 'own'
    assert len(calls) == 3 and all(c['proxies']['https'] == 'socks5h://127.0.0.1:18890' for c in calls)


def test_websocket_uses_socks_with_remote_dns_and_no_direct_fallback():
    from backend.app.adapters.xhs.im_protocol import XHSWebSocket
    received = []
    async def exercise():
        async def socks(reader, writer):
            try:
                version, methods = await reader.readexactly(2)
                assert version == 5
                await reader.readexactly(methods)
                writer.write(b'\x05\x00'); await writer.drain()
                request = await reader.readexactly(4)
                assert request == b'\x05\x01\x00\x03'
                size = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(size)).decode()
                port = int.from_bytes(await reader.readexactly(2), 'big')
                received.append((host, port))
                writer.write(b'\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00'); await writer.drain()
            finally:
                writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(socks, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        auth = SimpleNamespace(user_id='own', proxies={'https': f'socks5h://127.0.0.1:{port}'}, cookies_for_url=lambda url: {})
        connection = XHSWebSocket(auth, sid='synthetic', device_id='device', fingerprint='fingerprint',
            websocket_url='wss://xhs-test-host.invalid/rwp')
        try:
            with pytest.raises(Exception): await asyncio.wait_for(connection.connect(), timeout=3)
        finally:
            await connection.close()
            server.close(); await server.wait_closed()
    asyncio.run(exercise())
    assert received == [('xhs-test-host.invalid', 443)]


def test_worker_does_not_claim_or_connect_while_home_network_is_offline(home, monkeypatch):
    from backend.app.services import dm_worker
    states = []
    monkeypatch.setattr(dm_worker, 'state', lambda aid, state, error='': states.append(state))
    def forbidden(*args, **kwargs): pytest.fail('offline worker attempted a connection or queue claim')
    monkeypatch.setattr(dm_worker, 'connection_data', forbidden)
    monkeypatch.setattr(dm_worker, 'next_delivery', forbidden)
    async def exercise():
        task = asyncio.create_task(dm_worker.account_loop(1))
        try:
            for _ in range(50):
                if states: break
                await asyncio.sleep(.01)
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())
    assert states == ['network_paused']


def test_status_api_requires_login_and_does_not_expose_proxy_credentials(home):
    from backend.app.api.direct_messages import router
    home.xhs_proxy_url = 'socks5://private-user:private-password@127.0.0.1:18890'
    app = FastAPI(); app.include_router(router, prefix='/api')
    client = TestClient(app)
    assert client.get('/api/xhs/direct-messages/network').status_code == 401
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    response = client.get('/api/xhs/direct-messages/network')
    assert response.status_code == 200 and response.json()['mode'] == 'home'
    assert 'private-user' not in response.text and 'private-password' not in response.text


def test_recent_conversation_pagination_collects_new_peers_and_rejects_stalled_cursor():
    from backend.app.adapters.xhs.im_adapter import XhsImConnection
    connection = object.__new__(XhsImConnection);connection.expected_user_id = 'own'
    calls = []
    pages = [{'chats':[{'chat_user_id':'first'}], 'total':2, 'next_ts':100},
             {'chats':[{'chat_user_id':'second'}], 'total':2, 'next_ts':90}]
    def get(path):
        calls.append(path)
        return SimpleNamespace(json=lambda: {'success':True,'data':pages[len(calls)-1]})
    connection._signed_get = get
    assert connection.recent_peers() == {'first','second'}
    assert 'page=0' in calls[0] and 'next_ts=100' in calls[1] and 'page=1' in calls[1]
    calls.clear();pages[1] = {'chats':[{'chat_user_id':'first'}], 'total':3, 'next_ts':100}
    with pytest.raises(ValueError, match='分页未推进'): connection.recent_peers()
