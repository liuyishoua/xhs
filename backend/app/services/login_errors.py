"""Actionable login failures without reflecting transport secrets to the UI."""
import logging

from backend.app.services.xhs_network import HomeNetworkUnavailable

logger = logging.getLogger(__name__)


def login_failure_detail(exc: Exception, action: str) -> str:
    code = getattr(exc, "code", None)
    logger.warning("XHS login failed: action=%s exception=%s transport_code=%s",
                   action, type(exc).__name__, code if isinstance(code, int) else None)
    if isinstance(exc, HomeNetworkUnavailable):
        reason = "家庭网络出口不可用，请检查 Mac mini 和网络连接。"
    elif code == 28 or isinstance(exc, TimeoutError):
        reason = "连接小红书登录服务超时，请稍后重试，或在官方创作服务平台登录后通过 Cookie 导入。"
    elif isinstance(code, int):
        reason = "无法连接小红书登录服务，请检查家庭网络出口后重试。"
    elif "HTTP 406" in str(exc):
        reason = "小红书拒绝了当前登录请求（HTTP 406），请在官方创作服务平台完成登录后通过 Cookie 导入。"
    elif any(word in str(exc) for word in ("频繁", "次数", "限流")):
        reason = "小红书限制了请求频率，请稍后再试。"
    elif any(word in str(exc) for word in ("滑块", "人机", "captcha", "风险")):
        reason = "小红书要求安全验证，请在官方网页完成验证后通过 Cookie 导入。"
    elif "验证码" in str(exc) and any(word in str(exc) for word in ("错误", "失效", "过期", "不正确")):
        reason = "短信验证码不正确或已过期，请核对后重试。"
    else:
        reason = "小红书未接受此次登录请求，请稍后重试，或使用官方网页登录后导入 Cookie。"
    return f"{action}：{reason}"
