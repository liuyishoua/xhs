"""Creator login stays on the configured egress and reports safe failures."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.services import xhs_network


@pytest.fixture
def home(monkeypatch):
    config = SimpleNamespace(xhs_network_mode="home", xhs_proxy_url="socks5://127.0.0.1:18890")
    monkeypatch.setattr(xhs_network, "get_settings", lambda: config)
    return config


@pytest.mark.parametrize("method,args", [
    ("create_qrcode", ()),
    ("check_qrcode_status", ("qr", {})),
    ("create_phone_session", ("synthetic-phone",)),
    ("confirm_phone_login", ("synthetic-phone", "1234", {})),
    ("get_user_info", ({},)),
    ("exchange_from_user_cookies", ({},)),
])
def test_all_creator_login_stages_share_home_exit_and_close(home, monkeypatch, method, args):
    from apis import xhs_creator_login_apis as sdk
    from backend.app.adapters.xhs.creator_login_adapter import XhsCreatorLoginAdapter
    clients = []

    class Api:
        def __init__(self, **options):
            self.options = options
            self.closed = False
            clients.append(self)
        def close(self): self.closed = True
        def generate_init_cookies(self): return {"a1": "synthetic"}
        def generate_qrcode(self, cookies): return True, "ok", {"cookies": cookies, "qr_id": "qr", "qr_url": "https://example.test/qr"}
        def check_qrcode_status(self, qr_id, cookies): return True, "ok", cookies
        def send_phone_code(self, phone, cookies): return True, "ok", {}
        def login_by_phone(self, phone, code, cookies): return True, "ok", {"cookies": cookies}
        def get_user_info(self, cookies): return True, {"userId": "own"}, cookies
        def exchange_creator_session_from_user_cookies(self, cookies): return True, "ok", {"cookies": cookies}

    monkeypatch.setattr(sdk, "XHSCreatorLoginApi", Api)
    getattr(XhsCreatorLoginAdapter(), method)(*args)
    assert len(clients) == 1
    assert clients[0].options == {"proxies": {"http": "socks5h://127.0.0.1:18890", "https": "socks5h://127.0.0.1:18890"}}
    assert clients[0].closed


def test_creator_timeout_closes_client_without_direct_retry(home, monkeypatch):
    from apis import xhs_creator_login_apis as sdk
    from backend.app.adapters.xhs.creator_login_adapter import XhsCreatorLoginAdapter
    calls = []
    class Api:
        def __init__(self, **options): calls.append(options)
        def generate_init_cookies(self): raise TimeoutError("private-transport-data")
        def close(self): calls.append("closed")
    monkeypatch.setattr(sdk, "XHSCreatorLoginApi", Api)
    with pytest.raises(TimeoutError): XhsCreatorLoginAdapter().create_qrcode()
    assert len(calls) == 2 and calls[-1] == "closed"
    assert calls[0]["proxies"]["https"].startswith("socks5h:")
    home.xhs_proxy_url = ""
    with pytest.raises(xhs_network.HomeNetworkUnavailable): XhsCreatorLoginAdapter().create_qrcode()
    assert len(calls) == 2


def test_creator_operations_use_the_same_egress(home, monkeypatch):
    from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter
    from xhs_utils.xhs_creator import XHSCreatorAuth
    from apis import xhs_creator_apis
    calls = []
    def auth(cookies, **options):
        calls.append(options)
        return "auth"
    monkeypatch.setattr(XHSCreatorAuth, "from_cookie", auth)
    monkeypatch.setattr(xhs_creator_apis, "XHS_Creator_Apis", lambda auth: auth)
    assert XhsCreatorApiAdapter("synthetic")._api() == "auth"
    assert calls[0]["proxies"]["https"] == "socks5h://127.0.0.1:18890"


@pytest.mark.parametrize("path,body,error,expected", [
    ("creator/qrcode", {}, TimeoutError("private-url?cookie=secret"), "超时"),
    ("creator/qrcode", {}, RuntimeError("获取二维码失败 (HTTP 406, code=-1)"), "HTTP 406"),
    ("creator/phone/send-code", {"phone": "synthetic-phone"}, TimeoutError("private-cookie"), "超时"),
    ("creator/phone/send-code", {"phone": "synthetic-phone"}, RuntimeError("请求过于频繁"), "请求频率"),
    ("pc/phone/send-code", {"phone": "synthetic-phone"}, RuntimeError("private-cookie"), "未接受"),
])
def test_login_api_returns_actionable_errors_without_transport_details(path, body, error, expected):
    from backend.app.api import login_sessions
    from backend.app.core.database import get_db
    from backend.app.core.deps import get_current_user
    app = FastAPI()
    app.include_router(login_sessions.router)
    class Adapter:
        def create_qrcode(self): raise error
        def create_phone_session(self, phone): raise error
    app.dependency_overrides[get_db] = lambda: SimpleNamespace(add=lambda row: None, commit=lambda: None)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[login_sessions.get_creator_login_adapter] = Adapter
    app.dependency_overrides[login_sessions.get_pc_login_adapter] = Adapter
    response = TestClient(app).post("/xhs/login-sessions/" + path, json=body)
    assert response.status_code in (400, 502)
    assert expected in response.json()["detail"]
    assert "private" not in response.text and "secret" not in response.text
