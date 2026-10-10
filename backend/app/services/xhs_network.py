"""One explicit XHS egress for PC, Creator and IM."""
import asyncio
from datetime import datetime
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from backend.app.core.config import get_settings
from backend.app.core.time import SHANGHAI_TZ


class HomeNetworkUnavailable(ValueError):
    pass


_lock = threading.Lock()
_snapshot = {}
_checked_monotonic = 0.0
_configuration = None


def proxy_url():
    settings = get_settings()
    if settings.xhs_network_mode == 'direct':
        return None
    try:
        url = urlsplit(settings.xhs_proxy_url)
        valid = (url.scheme in ('socks5', 'socks5h') and url.hostname and url.port
                 and url.path in ('', '/') and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise HomeNetworkUnavailable('家庭网络出口未配置，已暂停小红书连接')
    # DNS also travels through the home endpoint.
    return urlunsplit(('socks5h', url.netloc, '', '', ''))


def http_options():
    proxy = proxy_url()
    return {'proxies': {'http': proxy, 'https': proxy}} if proxy else {}


def _config_key():
    settings = get_settings()
    return (settings.xhs_network_mode, settings.xhs_proxy_url)


def status():
    settings = get_settings()
    expires_at = settings.xhs_network_authorization_expires_at or None
    if expires_at:
        try:
            expires_at = datetime.fromisoformat(expires_at.replace('Z', '+00:00')).astimezone(SHANGHAI_TZ).isoformat()
        except ValueError:
            pass
    result = {
        'mode': settings.xhs_network_mode,
        'label': settings.xhs_network_label if settings.xhs_network_mode == 'home' else '服务器直连',
        'state': 'checking', 'message': '正在检测家庭网络，检测通过后连接',
        'checked_at': None, 'last_online_at': None, 'exit_ip': None,
        'authorization_expires_at': expires_at,
        'automatic_fallback': False,
    }
    if settings.xhs_network_mode == 'direct':
        return {**result, 'state': 'direct', 'message': '使用服务器出口，尚未确认接收方手机可见'}
    try:
        proxy_url()
    except HomeNetworkUnavailable as exc:
        return {**result, 'state': 'unconfigured', 'message': str(exc)}
    # Assignment of a new immutable snapshot is atomic; don't block the event
    # loop on the check lock while the bounded network probe is in progress.
    if _configuration == _config_key():
        result.update(_snapshot)
        if result['state'] == 'online' and time.monotonic() - _checked_monotonic > 45:
            result.update(state='offline', message='家庭网络检测已过期，暂停处理并等待重新连接')
    return result


def require_ready():
    current = status()
    if current['mode'] == 'home' and current['state'] != 'online':
        raise HomeNetworkUnavailable(current['message'])


def check():
    global _snapshot, _checked_monotonic, _configuration
    with _lock:
        current = status()
        if current['mode'] == 'direct' or current['state'] == 'unconfigured':
            return current
        observation = {'checked_at': datetime.now(SHANGHAI_TZ).isoformat(),
                       'last_online_at': current['last_online_at'], 'exit_ip': None}
        try:
            from curl_cffi import requests
            with requests.Session(**http_options(), impersonate='chrome') as client:
                response = client.head('https://www.xiaohongshu.com/', timeout=8, allow_redirects=False)
            if response.status_code in (403, 429, 461):
                observation.update(state='restricted', message='家庭出口收到平台限制响应，已暂停处理，请检查小红书登录和网络')
            elif response.status_code in (200, 301, 302, 404):
                observation.update(state='online', message='家庭网络可用，小红书 PC 和 Creator 流量固定从此出口访问',
                                   last_online_at=observation['checked_at'], exit_ip=response.headers.get('xhs-real-ip'))
            else:
                observation.update(state='offline', message='小红书连接检测未通过，暂停处理并自动重试')
        except Exception:
            # Do not expose proxy credentials, SDK exception URLs or cookies.
            observation.update(state='offline', message='家庭网络暂不可用，已暂停处理；请保持 Mac mini 和 Tailscale 在线')
        _snapshot = observation
        _checked_monotonic = time.monotonic()
        _configuration = _config_key()
        return status()


async def monitor():
    while True:
        await asyncio.to_thread(check)
        await asyncio.sleep(15)
