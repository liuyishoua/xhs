from __future__ import annotations

from typing import Any
from contextlib import contextmanager

from backend.app.adapters.xhs.request_env import direct_xhs_request_env
from backend.app.services import xhs_network


class XhsCreatorLoginAdapter:
    @contextmanager
    def _api(self):
        with direct_xhs_request_env():
            from apis.xhs_creator_login_apis import XHSCreatorLoginApi

            api = XHSCreatorLoginApi(**xhs_network.http_options())
            try:
                yield api
            finally:
                api.close()

    def exchange_from_user_cookies(self, user_cookies: dict[str, Any]) -> dict[str, Any]:
        with self._api() as api:
            success, message, payload = api.exchange_creator_session_from_user_cookies(dict(user_cookies))
        if not success or not payload:
            raise RuntimeError(message)
        return {"status": "confirmed", "cookies": payload["cookies"]}

    def create_qrcode(self) -> dict[str, Any]:
        with self._api() as api:
            cookies = api.generate_init_cookies()
            success, message, payload = api.generate_qrcode(cookies)
        if not success or not payload:
            raise RuntimeError(message)
        return {
            "cookies": payload["cookies"],
            "qr_id": payload["qr_id"],
            "qr_url": payload["qr_url"],
        }

    def check_qrcode_status(self, qr_id: str, cookies: dict[str, Any]) -> dict[str, Any]:
        with self._api() as api:
            success, message, updated_cookies = api.check_qrcode_status(qr_id, cookies)
        status = "confirmed" if success else "pending"
        if "过期" in message or "expired" in message.lower():
            status = "expired"
        if "确认" in message or "confirm" in message.lower():
            status = "scanned"
        return {"status": status, "cookies": updated_cookies}

    def get_user_info(self, cookies: dict[str, Any]) -> dict[str, Any]:
        with self._api() as api:
            success, data, refreshed = api.get_user_info(cookies)
            code = getattr(api, 'last_user_info_code', None)
            http_status = getattr(api, 'last_user_info_http_status', None)
        if not success:
            from backend.app.services.account_health import creator_session_failure
            raise creator_session_failure(code, http_status)
        if not data.get('userId'):
            raise RuntimeError('Creator profile has no account identity')
        refreshed = dict(refreshed)
        cookies.clear()
        cookies.update(refreshed)
        return {
            "external_user_id": data.get("userId", ""),
            "nickname": data.get("userName", ""),
            "avatar_url": data.get("userAvatar", ""),
            "profile": {
                "red_id": data.get("redId") or data.get("red_id") or "",
                "role": data.get("role") or "",
                "real_name_verified": data.get("realNameVerified"),
                "followers": data.get("fans") or data.get("followers") or data.get("followerCount"),
                "following": data.get("follows") or data.get("following") or data.get("followingCount"),
                "likes": data.get("likedCount") or data.get("likes") or data.get("likeCount"),
                "raw": data,
            },
        }

    def check_session(self, cookies: dict[str, Any]) -> dict[str, Any]:
        """Verify identity AND upload permission; this never uploads or posts."""
        from backend.app.adapters.xhs.creator_api_adapter import XhsCreatorApiAdapter
        from backend.app.services.account_health import CreatorUploadError, AccountCheckError
        from xhs_utils.xhs_core.cookies import cookie_header

        def verify(values):
            info = self.get_user_info(values)
            adapter = XhsCreatorApiAdapter(cookie_header(values))
            try:
                adapter.check_upload_permission()
            except CreatorUploadError as exc:
                raise exc.account_error from None
            values.update(adapter.refreshed_cookies or {})
            return info

        checked = dict(cookies)
        try:
            info = verify(checked)
        except AccountCheckError as exc:
            if exc.account_status != 'expired':
                raise
            # A short-lived Creator token can expire while its existing SSO
            # session remains valid. Ask CAS once, with the same credentials;
            # no SMS, anonymous session reset or risk-verification workaround.
            with self._api() as api:
                session = api.query_session(dict(cookies))
            if not session.get('active'):
                raise exc
            checked = dict(session['cookies'])
            info = verify(checked)
        cookies.clear()
        cookies.update(checked)
        return info

    def create_phone_session(self, phone: str) -> dict[str, Any]:
        with self._api() as api:
            cookies = api.generate_init_cookies()
            success, message, _ = api.send_phone_code(phone, cookies)
        if not success:
            raise RuntimeError(message)
        return {"cookies": cookies, "message": message or "sent"}

    def confirm_phone_login(self, phone: str, code: str, cookies: dict[str, Any]) -> dict[str, Any]:
        with self._api() as api:
            success, message, payload = api.login_by_phone(phone, code, cookies)
        if not success or not payload:
            raise RuntimeError(message)
        return {"status": "confirmed", "cookies": payload["cookies"]}
