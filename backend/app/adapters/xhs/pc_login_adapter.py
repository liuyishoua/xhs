from __future__ import annotations

from typing import Any
from time import monotonic

from backend.app.adapters.xhs.request_env import direct_xhs_request_env
from backend.app.services import xhs_network
from backend.app.services.account_health import profile_failure


class XhsPcLoginAdapter:
    def create_qrcode(self) -> dict[str, Any]:
        with direct_xhs_request_env():
            from apis.xhs_pc_login_apis import XHSLoginApi

            api = XHSLoginApi(**xhs_network.http_options())
            try:
                cookies = api.generate_init_cookies()
                success, message, payload = api.generate_qrcode(cookies)
            finally:
                api.close()
        if not success or not payload:
            raise RuntimeError(message)
        return {
            "cookies": payload["cookies"],
            "qr_id": payload["qr_id"],
            "code": payload["code"],
            "qr_url": payload["qr_url"],
        }

    def check_qrcode_status(self, qr_id: str, code: str, cookies: dict[str, Any]) -> dict[str, Any]:
        with direct_xhs_request_env():
            from apis.xhs_pc_login_apis import XHSLoginApi

            api = XHSLoginApi(**xhs_network.http_options())
            try:
                success, message, updated_cookies = api.check_qrcode_status(qr_id, code, cookies)
            finally:
                api.close()
        status = "confirmed" if success else "pending"
        if not success and ("过期" in message or "expired" in message.lower()):
            status = "expired"
        elif not success and ("确认" in message or "confirm" in message.lower()):
            status = "scanned"
        return {"status": status, "cookies": updated_cookies}

    def get_user_info(self, cookies: dict[str, Any]) -> dict[str, Any]:
        with direct_xhs_request_env():
            from apis.xhs_pc_login_apis import XHSLoginApi

            api = XHSLoginApi(**xhs_network.http_options())
            try:
                success, data, _ = api.get_user_info(cookies)
                if not success or data.get('guest') or not data.get('user_id'):
                    raise profile_failure({'code': api.last_user_info_code})
            finally:
                api.close()
        return {
            "external_user_id": data.get("user_id", ""),
            "nickname": data.get("nickname", ""),
            "avatar_url": data.get("images") or data.get("imageb") or "",
            "profile": {
                "red_id": data.get("red_id") or data.get("redId") or "",
                "followers": data.get("fans") or data.get("followers") or data.get("follower_count"),
                "following": data.get("follows") or data.get("following") or data.get("following_count"),
                "likes": data.get("liked_count") or data.get("likes") or data.get("like_count"),
                "raw": data,
            },
        }

    def prepare_phone_session(self):
        from apis.xhs_pc_login_apis import XHSLoginApi
        started = monotonic()
        with direct_xhs_request_env():
            api = XHSLoginApi(**xhs_network.http_options())
            try:
                cookies = api.generate_init_cookies()
                api.ensure_webprofile(cookies)
            except BaseException:
                api.close()
                raise
        return api, cookies, {"initialization_ms": round((monotonic() - started) * 1000)}

    def send_prepared_phone_code(self, phone, prepared):
        started = monotonic()
        self.last_phone_diagnostics = {"phase": "requesting_sms", "preparation_reused": True,
                                       "preparation_ms": prepared.timings["initialization_ms"]}
        try:
            with direct_xhs_request_env():
                entered = monotonic()
                self.last_phone_diagnostics["connection_wait_ms"] = round((entered - started) * 1000)
                success, message, response = prepared.api.send_phone_code(phone, prepared.cookies)
            self.last_phone_diagnostics["send_request_ms"] = round((monotonic() - entered) * 1000)
            self.last_phone_diagnostics["platform_success"] = success is True
            if isinstance(response, dict) and type(response.get("code")) is int:
                self.last_phone_diagnostics["platform_code"] = response["code"]
            if success is not True:
                raise RuntimeError(message or "小红书未确认验证码请求")
            self.last_phone_diagnostics["phase"] = "platform_accepted"
            return {"cookies": prepared.cookies, "message": message or "sent",
                    "pc_login_state": prepared.api.export_phone_state()}
        finally:
            prepared.api.close()
            self.last_phone_diagnostics["total_ms"] = round((monotonic() - started) * 1000)

    def create_phone_session(self, phone: str) -> dict[str, Any]:
        started = monotonic()
        self.last_phone_diagnostics = {"phase": "waiting_for_connection"}
        try:
            with direct_xhs_request_env():
                from apis.xhs_pc_login_apis import XHSLoginApi

                entered = monotonic()
                self.last_phone_diagnostics["connection_wait_ms"] = round((entered - started) * 1000)
                api = XHSLoginApi(**xhs_network.http_options())
                try:
                    self.last_phone_diagnostics["phase"] = "initializing_login"
                    cookies = api.generate_init_cookies()
                    # Match the SDK phone_login flow: report the anonymous
                    # profile before the send-code request reuses its b1/gid.
                    api.ensure_webprofile(cookies)
                    initialized = monotonic()
                    self.last_phone_diagnostics["initialization_ms"] = round((initialized - entered) * 1000)
                    self.last_phone_diagnostics["phase"] = "requesting_sms"
                    success, message, response = api.send_phone_code(phone, cookies)
                    self.last_phone_diagnostics["send_request_ms"] = round((monotonic() - initialized) * 1000)
                    code = response.get("code") if isinstance(response, dict) else None
                    if isinstance(code, int) and not isinstance(code, bool) and abs(code) <= 99999999:
                        self.last_phone_diagnostics["platform_code"] = code
                    elif isinstance(code, str) and code.lstrip("-").isdigit() and len(code) <= 8:
                        self.last_phone_diagnostics["platform_code"] = int(code)
                    self.last_phone_diagnostics["platform_success"] = success is True
                    if success is not True:
                        raise RuntimeError(message or "小红书未确认验证码请求")
                    self.last_phone_diagnostics["phase"] = "platform_accepted"
                    login_state = api.export_phone_state()
                finally:
                    api.close()
        finally:
            self.last_phone_diagnostics["total_ms"] = round((monotonic() - started) * 1000)
        return {"cookies": cookies, "message": message or "sent", "pc_login_state": login_state}

    def confirm_phone_login(self, phone: str, code: str, cookies: dict[str, Any], *, pc_login_state=None) -> dict[str, Any]:
        self.last_phone_diagnostics = {'phase': 'restoring_login_state', 'state_restored': False}
        with direct_xhs_request_env():
            from apis.xhs_pc_login_apis import XHSLoginApi

            api = XHSLoginApi(**xhs_network.http_options())
            try:
                if pc_login_state is not None:
                    api.restore_phone_state(cookies, pc_login_state)
                    self.last_phone_diagnostics['state_restored'] = True
                self.last_phone_diagnostics['phase'] = 'checking_code'
                success, message, payload = api.login_by_phone(phone, code, cookies)
            finally:
                self.last_phone_diagnostics.update(getattr(api, 'last_phone_diagnostics', {}))
                api.close()
        if not success or not payload:
            failure = RuntimeError(message)
            failure.phase = self.last_phone_diagnostics.get('phase')
            failure.platform_code = self.last_phone_diagnostics.get('platform_code')
            failure.http_status = self.last_phone_diagnostics.get('http_status')
            raise failure
        return {"status": "confirmed", "cookies": payload["cookies"]}
