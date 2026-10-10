"""Regression coverage for the real upload deadline and image metadata."""
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from apis import xhs_creator_apis as sdk
from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter


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
