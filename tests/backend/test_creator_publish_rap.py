"""The Creator RAP binds the complete HTTPS URL, as observed in its web client."""
import json
import subprocess
from types import SimpleNamespace

from apis import xhs_creator_apis as sdk
from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter
from xhs_utils.xhs_creator import XHSCreatorAuth


def _transport(captured):
    def post(url, **options):
        captured.update(url=url, headers=options["headers"], body=options["data"].decode())
        return SimpleNamespace(status_code=200, json=lambda: {"success": True, "data": {"id": "test-note"}})
    return SimpleNamespace(post=post)


def _request_params(api, data, method, **options):
    return {}, {}, json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _assert_full_url_hash(captured):
    # Decode the generated envelope independently of the Python caller. A path
    # argument previously made rap.js hash //edith..., which differs from HTTPS.
    script = r"""
const fs = require('fs');
const {decryptBody} = require('./xhs_utils/xhs_pc/js/rap_crypto.js');
const {xxHash32} = require('./xhs_utils/xhs_pc/js/rap.js');
const input = JSON.parse(fs.readFileSync(0,'utf8'));
const env = Buffer.from(input.headers['x-rap-param'],'base64');
const n = env[3], length = env.readUInt32BE(12)-4;
const plain = decryptBody(env.subarray(36+n,52+n), env.subarray(56+n,56+n+length));
let mask;
for (let i=0;i<256;i++) if (xxHash32(Buffer.from([i]))===plain.readUInt32BE(12)) mask=i;
if (mask===undefined) throw Error('Unknown RAP mask');
const fingerprint = Buffer.from(plain.subarray(16).map(x=>x^mask));
const slot = fingerprint.indexOf(Buffer.from('03eb','hex'));
const actual = fingerprint.readUInt32BE(slot+2);
console.log(JSON.stringify({actual,expected:xxHash32(Buffer.from(input.url+input.body)),
 old:xxHash32(Buffer.from(input.url.replace('https:','')+input.body))}));
"""
    result = subprocess.run(["node", "-e", script], input=json.dumps(captured), text=True,
                            capture_output=True, check=True, timeout=10)
    values = json.loads(result.stdout)
    assert values["actual"] == values["expected"]
    assert values["actual"] != values["old"]


def test_preuploaded_image_publish_rap_hash_matches_destination(monkeypatch):
    captured = {}
    api = SimpleNamespace(base_url="https://creator.xiaohongshu.com", edith_url="https://edith.xiaohongshu.com",
                          _request_params=_request_params, http=_transport(captured))
    monkeypatch.setattr(XHSCreatorAuth, "from_cookie", lambda *args, **kwargs: object())
    monkeypatch.setattr(sdk, "XHS_Creator_Apis", lambda auth: api)
    result = XhsCreatorApiAdapter("synthetic").post_note({
        "title": "test", "media_type": "image", "image_file_infos": [{"fileIds": "test-image", "width": 40, "height": 60}],
    })
    assert result["success"]
    _assert_full_url_hash(captured)


def test_sdk_image_publish_rap_hash_matches_destination():
    captured = {}
    api = object.__new__(sdk.XHS_Creator_Apis)
    api.base_url = "https://creator.xiaohongshu.com"
    api.edith_url = "https://edith.xiaohongshu.com"
    api.auth = SimpleNamespace(proxies=None)
    api.http = _transport(captured)
    api._request_params = _request_params
    api.upload_media = lambda *args, **kwargs: (True, "ok", {"fileIds": "test-image", "width": 40, "height": 60})
    success, message, result = api.post_note({"title": "test", "images": [b"test-image"]})
    assert success, message
    _assert_full_url_hash(captured)
