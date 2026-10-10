from __future__ import annotations

from typing import Any

from backend.app.adapters.xhs.request_env import direct_xhs_request_env
from backend.app.services import xhs_network


class XhsPcApiAdapter:
    def __init__(self, cookies: str) -> None:
        self.cookies = cookies

    def _api(self):
        from apis.xhs_pc_apis import XHS_Apis
        from xhs_utils.xhs_pc import XHSPcAuth

        auth = XHSPcAuth.from_cookie(self.cookies, **xhs_network.http_options())
        return XHS_Apis(auth)

    def search_note(
        self,
        keyword: str,
        page: int = 1,
        sort_type_choice: int = 0,
        note_type: int = 0,
        note_time: int = 0,
        note_range: int = 0,
        pos_distance: int = 0,
        geo: str = "",
    ) -> Any:
        with direct_xhs_request_env():
            return self._api().search_note(
                query=keyword,
                page=page,
                sort_type_choice=sort_type_choice,
                note_type=note_type,
                note_time=note_time,
                note_range=note_range,
                pos_distance=pos_distance,
                geo=geo,
            )

    def get_note_info(self, url: str) -> Any:
        with direct_xhs_request_env():
            return self._api().get_note_info(url=url)

    def get_note_comments(self, note_url: str) -> Any:
        with direct_xhs_request_env():
            return self._api().get_note_all_comment(url=note_url)

    def get_user_notes(self, user_url: str) -> Any:
        with direct_xhs_request_env():
            return self._api().get_user_all_notes(user_url=user_url)

    def get_self_info(self) -> Any:
        with direct_xhs_request_env():
            success, message, res_json = self._api().get_user_me()
        payload = (res_json or {}).get("data") if isinstance(res_json, dict) else None
        if not success or not payload:
            raise RuntimeError(message or "XHS self profile refresh failed")
        return payload

    def get_mentions(self, cursor: str = "") -> dict[str, Any]:
        with direct_xhs_request_env():
            success, _, payload = self._api().get_metions(cursor)
        if not success or not isinstance(payload, dict):
            raise RuntimeError("小红书评论通知读取失败")
        return payload.get("data") or {}

    def post_comment(self, note_id: str, content: str, target_comment_id: str = "") -> dict[str, Any]:
        # The caller binds identity/target before invoking this transport.
        from xhs_utils.http_util import REQUEST_TIMEOUT
        body = {"note_id": note_id, "content": content, "at_users": []}
        if target_comment_id:
            body["target_comment_id"] = target_comment_id
        with direct_xhs_request_env():
            api = self._api()
            path = "/api/sns/web/v1/comment/post"
            headers, cookies, data = api._request_params(path, body, "POST")
            response = api.http.post(api.base_url + path, headers=headers,
                                     cookies=cookies, data=data.encode('utf-8'), timeout=REQUEST_TIMEOUT)
            payload = response.json()
        if payload.get("success") is not True:
            raise RuntimeError("小红书评论提交未确认，code=" + str(payload.get("code")))
        return payload

    def get_recent_user_notes(self, user_id: str) -> dict[str, Any]:
        with direct_xhs_request_env():
            success, _, payload = self._api().get_user_note_info(user_id, '')
        if not success or not isinstance(payload, dict):
            raise RuntimeError('小红书最新笔记读取失败')
        return payload.get('data') or {}
