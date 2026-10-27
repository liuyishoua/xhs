"""Current PC-cookie IM transport, sharing the workspace's signing implementation."""
import asyncio
import json
import time
from backend.app.adapters.xhs.im_protocol import BROWSER_UA, XHSWebSocket
from xhs_utils.xhs_pc import XHSPcAuth
from xhs_utils.xhs_pc.params import generate_request_params, build_pc_business_headers
from apis.xhs_pc_apis import XHS_Apis
from backend.app.services import xhs_network
from backend.app.services.account_health import profile_failure, AccountCheckError


class XhsImConnection:
    def __init__(self, cookies, expected_user_id):
        self.auth = XHSPcAuth.from_cookie(cookies, **xhs_network.http_options())
        self.expected_user_id = expected_user_id
        self.nickname = ''
        self.socket = None

    def bootstrap(self):
        self.refresh_login()
        state = self.auth.profile.session.snapshot()
        self.auth.session_storage = {**dict(self.auth.session_storage or {}), **state}
        response = self._signed_get('/api/sns/web/v1/celestial/lt', device_id=state['XHS_TAB_DEVICE_ID'])
        body = response.json()
        data = body.get('data') or {}
        token = data.get('aLt') or data.get('a_lt')
        if not body.get('success') or not token:
            raise ValueError('小红书私信认证失败，请检查 PC 登录状态')
        ttl = int(data.get('expiredTime') or data.get('expired_time') or 600)
        self.auth.local_storage = {**dict(self.auth.local_storage or {}), 'RWP_LOGIN_TOKEN': json.dumps({
            'aLt': token, 'uid': self.auth.user_id, 'expiredAt': int(time.time()*1000) + ttl*500})}

    def cookie_snapshot(self):
        return self.auth.cookies_for_url(self.auth.origin('api'))

    def refresh_login(self):
        success, _, payload = XHS_Apis(self.auth).get_user_me()
        profile = (payload or {}).get('data') or {}
        user_id = profile.get('user_id')
        if not success or not user_id or profile.get('guest'):
            raise profile_failure(payload)
        if str(user_id) != str(self.expected_user_id):
            raise AccountCheckError('risk', '私信 Cookie 与绑定账号不一致，请重新绑定 PC 账号')
        self.auth.set_user_id(str(user_id))
        self.nickname = str(profile.get('nickname') or '')

    def _signed_get(self, api, *, device_id=None):
        return self._signed_request('GET', api, device_id=device_id)

    def _signed_request(self, method, api, data='', *, device_id=None, content_type=None):
        ctx = self.auth.next_sign_context(api)
        headers, cookies, wire_data = generate_request_params(self.auth.cookies, api, data, method,
            user_id=self.auth.user_id, b1=self.auth.current_b1(ctx['now']), dsl_pair=self.auth.dsl_pair,
            doc_cookie=self.auth.sign_cookie, tier=ctx['tier'], sign_context=ctx)
        target = self.auth.origin('api') + api
        wire = self.auth.cookies_for_url(target, cookies)
        headers = build_pc_business_headers(headers, wire, api=api, method=method)
        # The sibling SDK's captured celestial request places c_device_id after
        # x-xray-traceid and omits client hints. Keep the existing signer intact.
        headers = {key: value for key, value in headers.items() if not key.startswith('sec-ch-')}
        headers['user-agent'] = BROWSER_UA
        ordered = {}
        for key, value in headers.items():
            ordered[key] = value
            if key == 'x-xray-traceid' and device_id:
                ordered['c_device_id'] = device_id
        headers = ordered
        if content_type:
            headers['content-type'] = content_type
        response = getattr(self.auth.http_client, method.lower())(target, headers=headers, cookies=wire,
            **({'data': wire_data} if method != 'GET' else {}), proxies=self.auth.proxies, timeout=20)
        # A logout response can replace web_session with an anonymous session.
        # Never persist that as a renewal of the authenticated identity.
        if response.json().get('success') is True and getattr(response, 'cookies', None) is not None:
            updates = dict(response.cookies.items())
            if updates:
                self.auth.update_cookies(updates, source_url=target)
        return response

    def history_page(self, peer_id, *, last_id=0, limit=100):
        from urllib.parse import urlencode
        api = '/api/im/web/messages/history?' + urlencode({'chat_user_id': peer_id, 'last_id': last_id, 'start_id': 0, 'limit': limit})
        response = self._signed_get(api)
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('success') is not True:
            raise ValueError('小红书聊天历史读取失败，请检查账号登录状态')
        return payload.get('data')

    def recent_peers(self, checkpoints=None, since=None):
        from urllib.parse import urlencode
        from backend.app.services.dm_history import timestamp, HISTORY_WINDOW_HOURS
        from backend.app.core.time import SHANGHAI_TZ, shanghai_now
        from datetime import datetime, timedelta
        peers, cursor, seen_chats = set(), None, 0
        self.recent_chat_summaries = {}
        self.history_refresh_peers = set()
        self.chat_summary_pages = 0
        cutoff = since or shanghai_now() - timedelta(hours=HISTORY_WINDOW_HOURS)
        for page in range(30):
            params = {'limit': 100, 'complete': 'true', 'page': page, 'source': 'pc'}
            if cursor is not None:
                params['next_ts'] = cursor
            payload = self._signed_get('/api/im/web/v3/chats?' + urlencode(params)).json()
            self.chat_summary_pages += 1
            data = payload.get('data') or {}
            chats = data.get('chats')
            if payload.get('success') is not True or not isinstance(chats, list):
                raise ValueError('最近会话读取失败，尚未完成断线消息补收')
            seen_chats += len(chats)
            for chat in chats:
                peer = chat.get('chat_user_id') if isinstance(chat, dict) else None
                if isinstance(peer, str) and peer and len(peer) <= 128 and peer != self.expected_user_id:
                    times = [timestamp(chat.get(k)) for k in ('last_msg_time', 'update_time')]
                    activity = max((t for t in times if t is not None), default=None)
                    max_id = chat.get('max_store_id')
                    self.recent_chat_summaries[peer] = {'max_store_id': max_id,
                        'update_time': chat.get('update_time'), 'last_msg_time': chat.get('last_msg_time')}
                    info=chat.get('info') or {}
                    for field in ('nickname','avatar'):
                        value=info.get(field) or (info.get('user_name') if field=='nickname' else '')
                        if isinstance(value,str):self.recent_chat_summaries[peer][field]=value[:500]
                    if checkpoints is not None:
                        cached = checkpoints.get(peer) or {}
                        try:
                            until = datetime.fromisoformat(cached['until']) if cached.get('complete') else None
                            if until is not None and until.tzinfo is not None:
                                until = until.astimezone(SHANGHAI_TZ).replace(tzinfo=None)
                        except (KeyError, TypeError, ValueError):
                            until = None
                        cached_id = cached.get('max_store_id')
                        advanced = type(max_id) is int and type(cached_id) is int and max_id > cached_id
                        if activity is not None and activity < cutoff and not advanced:
                            continue
                        if until is not None and activity is not None and activity <= until and not advanced:
                            continue
                        if until is not None and activity is not None and activity > until and not advanced:
                            # An edit/revocation can change a chat without adding
                            # a new store ID. Refresh its retained history then.
                            self.history_refresh_peers.add(peer)
                    peers.add(peer)
            total = data.get('total')
            if not chats or (isinstance(total, int) and seen_chats >= total):
                return peers
            next_cursor = data.get('next_ts')
            if next_cursor is None or next_cursor == cursor:
                raise ValueError('最近会话分页未推进，尚未完成断线消息补收')
            cursor = next_cursor
        raise ValueError('最近会话超过本轮补收上限，已暂停自动回复')

    def upload_image(self, path):
        """Official IM permit -> ROS object upload -> signed CDN conversion."""
        import hashlib
        import hmac
        import uuid
        from urllib.parse import urlencode, quote
        from PIL import Image
        from backend.app.services.dm_media import validate_image, safe_url, MediaUnavailable
        blob = path.read_bytes()
        ext, mime = validate_image(blob)
        params = dict(version='1', biz_name='matrix', scene='im', file_count=1, file_format=ext)
        payload = self._signed_get('/api/sns/web/upload/permit?' + urlencode(params)).json()
        permits = (payload.get('data') or {}).get('uploadTempPermits') or []
        if payload.get('success') is not True or not permits:
            raise MediaUnavailable('小红书图片上传授权未完成')
        permit = permits[0]
        host = permit.get('uploadAddr')
        ids = permit.get('fileIds') or []
        if host != 'ros-upload.xiaohongshu.com' or not ids or not isinstance(ids[0], str) or ids[0].startswith('/') or '..' in ids[0]:
            raise MediaUnavailable('小红书返回了不支持的上传通道')
        key = ids[0]
        key_time = str(int(permit['startTime'] / 1000)) + ';' + str(int(permit['expireTime'] / 1000))
        secret_key = str(permit.get('secretKey') or 'null')
        secret_id = str(permit.get('secretId') or 'null')
        sign_key = hmac.new(secret_key.encode(), key_time.encode(), hashlib.sha1).hexdigest()
        canonical = 'put\n/' + key + '\n\nhost=' + host + '\n'
        to_sign = 'sha1\n' + key_time + '\n' + hashlib.sha1(canonical.encode()).hexdigest() + '\n'
        signature = hmac.new(sign_key.encode(), to_sign.encode(), hashlib.sha1).hexdigest()
        auth = 'q-sign-algorithm=sha1&q-ak=' + secret_id + '&q-sign-time=' + key_time + '&q-key-time=' + key_time + '&q-header-list=host&q-url-param-list=&q-signature=' + signature
        # Temporary upload authorization is sent only to the official ROS host.
        response = self.auth.http_client.put('https://' + host + '/' + quote(key, safe='/'), data=blob,
            headers={'Authorization': auth, 'x-cos-security-token': permit['token'], 'content-type': mime,
                     'origin': 'https://www.xiaohongshu.com', 'user-agent': BROWSER_UA},
            proxies=self.auth.proxies, timeout=60, allow_redirects=False)
        if response.status_code not in (200, 201, 204):
            raise MediaUnavailable('小红书图片上传未完成，HTTP ' + str(response.status_code))
        marker = str(uuid.uuid4())
        data = urlencode(dict(biz_code=5, scene='im', extra_info=json.dumps({'uuid': marker}, separators=(',', ':')),
                              file_id=key, source_type=permit['cloudType'], uuid=marker))
        converted = self._signed_request('POST', '/api/im/web/file/cdn_url', data,
            content_type='application/x-www-form-urlencoded; charset=utf-8').json()
        url = (converted.get('data') or {}).get('cdn_url') or (converted.get('data') or {}).get('cdnUrl')
        if converted.get('success') is not True or not url:
            raise MediaUnavailable('小红书图片地址转换未完成')
        url = safe_url(url)
        with Image.open(path) as image:
            width, height = image.size
        return {'url': url, 'file_id': key, 'width': width, 'height': height, 'bytes': len(blob)}

    async def connect(self):
        await asyncio.to_thread(self.bootstrap)
        self.socket = XHSWebSocket.from_auth_storage(self.auth)
        await asyncio.wait_for(self.socket.connect(), timeout=30)
        return self

    def events(self):
        return self.socket.events()

    async def heartbeat(self):
        await self.socket.send_transport_heartbeat()

    async def send(self, peer, text, mid, reply=None):
        # Caller persists mid and sending intent before invoking this method.
        return await self.socket.send_chat_message(mid=mid, ts=int(time.time()*1000), token='',
            sender=self.auth.user_id, receiver=peer, content=reply['content'] if reply else text, content_type=reply['content_type'] if reply else 1, nickname=self.nickname,
            group_chat=False, command=None, ref_id='', trigger_source=1)

    async def close(self):
        if self.socket:
            await self.socket.close()
        self.auth.close()
