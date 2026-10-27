"""Regression coverage for the real upload deadline and image metadata."""
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from apis import xhs_creator_apis as sdk
from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter


@pytest.mark.parametrize('current', [True, False])
def test_publish_upload_risk_reports_recovery_only_for_current_login(monkeypatch, current):
    from unittest.mock import Mock
    from backend.app.api.publish import _record_upload_session_failure
    from backend.app.services import account_service
    from backend.app.services.account_health import CreatorUploadError
    account = SimpleNamespace(id=4, status='active', status_message='')
    persist = Mock(return_value=current)
    report = Mock()
    monkeypatch.setattr(account_service, 'persist_refreshed_account_cookies', persist)
    monkeypatch.setattr(account_service, 'record_session_event', report)
    error = CreatorUploadError({'business_code': 906, 'http_status': 401})
    db = object()
    _record_upload_session_failure(db, account, 'a1=synthetic', error)
    persist.assert_called_once_with(db, 4, {'a1': 'synthetic'}, {'a1': 'synthetic'})
    if current:
        assert account.status == 'risk'
        report.assert_called_once_with(db, account, 'creator_upload', 'risk', business_code=906)
    else:
        assert account.status == 'active'
        report.assert_not_called()


def test_publish_transport_failure_does_not_start_account_recovery(monkeypatch):
    from unittest.mock import Mock
    from backend.app.api.publish import _record_upload_session_failure
    from backend.app.services import account_service
    report = Mock()
    monkeypatch.setattr(account_service, 'record_session_event', report)
    account = SimpleNamespace(id=4, status='active')
    _record_upload_session_failure(object(), account, 'a1=synthetic', TimeoutError('network'))
    assert account.status == 'active'
    report.assert_not_called()


@pytest.fixture
def upload_api(monkeypatch):
    api = object.__new__(sdk.XHS_Creator_Apis)
    api.auth = SimpleNamespace(
        proxies={"https": "socks5h://127.0.0.1:18890"},
        profile=SimpleNamespace(release={"userAgent": "test-agent"}),
    )
    api.upload_url = "https://fallback.example.test"
    permit = {"data": {"uploadTempPermits": [{
        "uploadAddr": "upload.example.test",
        "fileIds": ["spectrum/test-image"],
        "expireTime": 2000000000000,
        "token": "synthetic-token",
    }]}}
    api.get_fileIds = lambda *args, **kwargs: (True, "ok", (permit, 1900000000000))
    monkeypatch.setattr(sdk, "signature_js", SimpleNamespace(call=lambda *args: "test-signature"))
    monkeypatch.setattr(sdk, "_log_api_error", lambda exc: str(exc))
    return api


@pytest.mark.parametrize("image_format,mime", [("JPEG", "image/jpeg"), ("PNG", "image/png")])
def test_slow_upload_keeps_home_proxy_and_correct_image_metadata(upload_api, image_format, mime):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), "pink").save(buffer, format=image_format)
    raw = buffer.getvalue()

    def slow_upload(url, **options):
        # A 20-second transfer must fit, unlike the former 15-second deadline.
        if options["timeout"] < 20:
            raise TimeoutError("transfer exceeded request deadline")
        assert options["timeout"] <= 90
        assert options["proxies"] == upload_api.auth.proxies
        assert options["data"] == raw
        assert "cookies" not in options
        assert url == "https://upload.example.test/spectrum/test-image"
        return SimpleNamespace(raise_for_status=lambda: None)

    upload_api.http = SimpleNamespace(put=slow_upload)
    success, message, payload = upload_api.upload_media(raw, "image")
    assert success, message
    assert payload["mime_type"] == mime
    assert (payload["width"], payload["height"]) == (40, 60)
    assert payload["file_size"] == len(raw)


def test_upload_permit_failure_never_sends_media(upload_api):
    upload_api.get_fileIds = lambda *args, **kwargs: (False, "HTTP 406", (None, None))
    upload_api.http = SimpleNamespace(put=lambda *args, **kwargs: pytest.fail("must not upload"))
    success, message, payload = upload_api.upload_media(b"unused", "image")
    assert not success and payload is None
    assert "获取上传凭证失败" in message and "HTTP 406" in message


def test_transfer_timeout_is_not_reported_as_a_publish_failure(upload_api):
    def timeout(*args, **kwargs):
        raise TimeoutError("transfer deadline reached")
    upload_api.http = SimpleNamespace(put=timeout)
    success, message, payload = upload_api.upload_media(b"video", "video")
    assert not success and payload is None
    assert message == "素材传输失败: transfer deadline reached"


@pytest.mark.parametrize("prefix", ["Cookie: ", "cookie: ", "COOKIE:", ""])
def test_pasted_cookie_header_preserves_cookie_values(prefix):
    adapter = XhsCreatorApiAdapter(prefix + "gid=synthetic; a1=value:with:colon")
    assert adapter.cookies == "gid=synthetic; a1=value:with:colon"


def test_risk_permit_reports_actual_failure_and_never_uploads(upload_api):
    # Exercise the SDK implementation rather than the fixture's permit stub.
    del upload_api.get_fileIds
    upload_api.base_url = 'https://creator.xiaohongshu.com'
    upload_api._request_params = lambda *a, **kw: ({'x-t': '1900000000000'}, {}, '')
    upload_api.http = SimpleNamespace(
        get=lambda *a, **kw: SimpleNamespace(status_code=401, json=lambda: {
            'success': False, 'code': 906, 'msg': '账号存在风险，请重新登录', 'private': 'secret'}),
        put=lambda *a, **kw: pytest.fail('risk rejection must not upload'))
    ok, message, result = upload_api.upload_media(b'unused', 'image')
    assert not ok and result is None
    assert '906' in message and '重新登录' in message and '成功' not in message
    assert upload_api.last_upload_failure == {'phase': 'permit', 'http_status': 401, 'business_code': 906}
    assert 'secret' not in str(upload_api.last_upload_failure)


def test_upload_adapter_preserves_risk_code_and_closes(monkeypatch):
    from backend.app.services.account_health import CreatorUploadError
    closed = []
    api = SimpleNamespace(
        upload_media=lambda *a: (False, 'misleading SDK message', None),
        last_upload_failure={'phase': 'permit', 'http_status': 401, 'business_code': 906},
        auth=SimpleNamespace(close=lambda: closed.append(True)))
    adapter = XhsCreatorApiAdapter('a1=synthetic')
    monkeypatch.setattr(adapter, '_api', lambda: api)
    monkeypatch.setattr(adapter, '_resolve_file_data', lambda p: b'unused')
    with pytest.raises(CreatorUploadError) as caught:
        adapter.upload_media('image.jpg', 'image')
    assert caught.value.account_error.account_status == 'risk'
    assert caught.value.account_error.business_code == 906
    assert closed == [True]
