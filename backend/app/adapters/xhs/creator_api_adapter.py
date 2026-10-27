from __future__ import annotations

from typing import Any

from backend.app.adapters.xhs.request_env import direct_xhs_request_env
from backend.app.services import xhs_network


class XhsCreatorApiAdapter:
    def __init__(self, cookies: str) -> None:
        # A copied HTTP Cookie header may include its field name.
        cookies = cookies.strip()
        self.cookies = cookies[7:].lstrip() if cookies[:7].lower() == "cookie:" else cookies
        self.refreshed_cookies = None

    def check_upload_permission(self) -> None:
        from backend.app.services.account_health import CreatorUploadError
        with direct_xhs_request_env():
            api = self._api()
            try:
                success, _, _ = api.get_fileIds('image')
                if not success:
                    raise CreatorUploadError(api.last_upload_failure or {})
                self.refreshed_cookies = dict(api.auth.cookie_map)
            finally:
                api.auth.close()

    def _api(self):
        from apis.xhs_creator_apis import XHS_Creator_Apis
        from xhs_utils.xhs_creator import XHSCreatorAuth

        auth = XHSCreatorAuth.from_cookie(self.cookies, **xhs_network.http_options())
        return XHS_Creator_Apis(auth)

    def get_topic(self, keyword: str) -> Any:
        with direct_xhs_request_env():
            return self._api().get_topic(keyword=keyword)

    def get_location_info(self, keyword: str) -> Any:
        with direct_xhs_request_env():
            return self._api().get_location_info(keyword=keyword)

    def get_published_notes(self) -> Any:
        with direct_xhs_request_env():
            return self._api().get_all_posted_notes()

    def upload_media(self, file_path: str, media_type: str) -> dict[str, Any]:
        file_data = self._resolve_file_data(file_path)
        with direct_xhs_request_env():
            api = self._api()
            try:
                success, message, payload = api.upload_media(file_data, media_type)
                failure = getattr(api, 'last_upload_failure', None)
                if success:
                    self.refreshed_cookies = dict(api.auth.cookie_map)
            finally:
                api.auth.close()
        if not success:
            if failure:
                from backend.app.services.account_health import CreatorUploadError
                raise CreatorUploadError(failure)
            raise RuntimeError(message or "Creator media upload failed")
        return payload or {}

    @staticmethod
    def _resolve_file_data(file_path: str) -> bytes:
        import pathlib
        raw_bytes: bytes | None = None

        if file_path.startswith("http://") or file_path.startswith("https://"):
            import requests
            resp = requests.get(file_path, timeout=30, headers={"Referer": ""})
            resp.raise_for_status()
            raw_bytes = resp.content
        elif file_path.startswith("/api/files/media/"):
            from backend.app.core.config import get_settings
            file_name = file_path.split("/")[-1]
            local = pathlib.Path(get_settings().storage_dir) / "media" / file_name
            if local.is_file():
                raw_bytes = local.read_bytes()
        else:
            p = pathlib.Path(file_path)
            if p.is_file():
                raw_bytes = p.read_bytes()

        if raw_bytes is None:
            raise FileNotFoundError(f"素材文件不存在: {file_path}")

        lower = file_path.lower()
        if lower.endswith(".webp") or (len(raw_bytes) > 4 and raw_bytes[:4] == b"RIFF"):
            try:
                import io
                from PIL import Image
                img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=92)
                raw_bytes = buf.getvalue()
            except Exception:
                pass

        return raw_bytes

    def post_note(self, note_info: dict[str, Any]) -> dict[str, Any]:
        with direct_xhs_request_env():
            if note_info.get("media_type") == "image" and note_info.get("image_file_infos"):
                return self._post_uploaded_image_note(note_info)

            success, message, payload = self._api().post_note(note_info)
        if not success:
            raise RuntimeError(message or "Creator note publish failed")
        return payload or {}

    def _post_uploaded_image_note(self, note_info: dict[str, Any]) -> dict[str, Any]:
        from apis.xhs_creator_apis import XHS_Creator_Apis
        from xhs_utils.http_util import REQUEST_TIMEOUT
        from xhs_utils.xhs_creator import XHSCreatorAuth
        from xhs_utils.xhs_creator_util import (
            get_post_note_image_data,
            load_creator_rap_fingerprint_hex,
        )
        from xhs_utils.xhs_pc.params import generate_x_rap_param

        auth = XHSCreatorAuth.from_cookie(self.cookies, **xhs_network.http_options())
        api = XHS_Creator_Apis(auth)
        post_api = "/web_api/sns/v2/note"
        post_loc = {}
        location = note_info.get("location")
        if isinstance(location, dict):
            post_loc = location
        elif isinstance(location, str) and location.strip():
            success, message, location_info = api.get_location_info(location.strip())
            if not success:
                raise RuntimeError(message or "Creator location lookup failed")
            poi_list = (location_info.get("data") or {}).get("poi_list") or []
            if not poi_list:
                raise RuntimeError("未找到该地点")
            poi = poi_list[0]
            post_loc = {
                "name": poi["name"],
                "subname": poi["full_address"],
                "poi_id": poi["poi_id"],
                "poi_type": poi["poi_type"],
            }
        data = get_post_note_image_data(
            note_info.get("title", ""),
            note_info.get("desc", ""),
            note_info.get("postTime"),
            post_loc,
            note_info.get("type", 1),
            note_info["image_file_infos"],
        )
        # 浏览器实抓：未选地点时省略 post_loc 键，空对象会被服务端以参数错误打回
        if not post_loc:
            data["common"].pop("post_loc", None)
        for topic in note_info.get("topics") or []:
            if not isinstance(topic, str) or not topic.strip():
                continue
            success, message, topic_payload = api.get_topic(topic.strip())
            if not success:
                raise RuntimeError(message or "Creator topic lookup failed")
            topic_items = (topic_payload.get("data") or {}).get("topic_info_dtos") or []
            if not topic_items:
                raise RuntimeError(f"未找到话题{topic}")
            item = topic_items[0]
            insert_topic = {
                "id": item["id"],
                "link": item.get("link", ""),
                "name": item["name"],
                "type": "topic",
            }
            data["common"]["hash_tag"].append(insert_topic)
            data["common"]["desc"] += f" #{insert_topic['name']}[话题]# "
        headers, cookies, body = api._request_params(
            post_api,
            data,
            "POST",
            referer=f"{api.base_url}/",
            target_origin=api.edith_url,
            order_wire_headers=False,
        )
        headers["x-rap-param"] = generate_x_rap_param(
            api.edith_url + post_api,
            body,
            fingerprint_hex=load_creator_rap_fingerprint_hex(),
        )
        response = api.http.post(
            api.edith_url + post_api,
            headers=headers,
            data=body.encode("utf-8"),
            cookies=cookies,
            timeout=REQUEST_TIMEOUT,
        )
        payload = response.json()
        if not payload.get("success"):
            raise RuntimeError(
                "小红书提交笔记失败"
                f"（HTTP {response.status_code}, code={payload.get('code', payload.get('result'))}）: "
                + (payload.get("msg") or payload.get("message") or "平台未返回具体原因")
            )
        return payload
