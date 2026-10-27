"""Dependency-free server-side Channel client. Never exposes source credentials to browsers."""
import json
from pathlib import Path
import secrets
import urllib.error
import urllib.parse
import urllib.request


class ChannelError(ValueError):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def compact_context(context, maximum=22000):
    truncated = False
    def trim(value):
        nonlocal truncated
        if isinstance(value, str) and len(value) > 2000:
            truncated = True
            return value[:2000]
        if isinstance(value, dict):
            return {k: trim(v) for k,v in value.items()}
        if isinstance(value, list):
            return [trim(v) for v in value]
        return value
    result = trim(context)
    while len(json.dumps(result,ensure_ascii=False).encode()) > maximum and isinstance(result.get('messages'),list) and len(result['messages']) > 1:
        result['messages'].pop(0)
        truncated = True
    if truncated:
        result['context_truncated'] = True
    if len(json.dumps(result,ensure_ascii=False).encode()) > maximum:
        raise ChannelError('业务上下文过大，请减少本次请求的资料')
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ChannelClient:
    def __init__(self, config_file):
        path = Path(config_file)
        if not path.is_file():
            raise ChannelError('此工作台尚未配置 Channel 接入')
        config = json.loads(path.read_text())
        self.base = config['base_url'].rstrip('/')
        self.source = config['source_id']
        self.token = config['token']
        u = urllib.parse.urlsplit(self.base)
        if u.scheme != 'https' and not (u.scheme == 'http' and u.hostname in ('127.0.0.1', 'localhost', '::1')):
            raise ChannelError('Channel 必须使用 HTTPS 或本机地址')
        self.opener = urllib.request.build_opener(NoRedirect(), *([urllib.request.ProxyHandler({})] if u.hostname in ('127.0.0.1', 'localhost', '::1') else []))

    def request(self, method, path, data=None, body=None, content_type='application/json'):
        if body is None and data is not None:
            body = json.dumps(data, ensure_ascii=False).encode()
        request = urllib.request.Request(self.base + path, body, {'Authorization': 'Bearer ' + self.token, 'Content-Type': content_type}, method=method)
        try:
            with self.opener.open(request, timeout=20) as response:
                return json.loads(response.read(2 * 1024 * 1024))
        except urllib.error.HTTPError as exc:
            raise ChannelError('Channel 返回 HTTP ' + str(exc.code) + '；请在消息中枢检查请求或来源配置', status=exc.code) from None
        except (OSError, ValueError, urllib.error.URLError):
            raise ChannelError('Channel 连接未完成；如提交响应丢失，请保留原消息 ID 重试') from None

    def send(self, account_id, message_id, text='', images=None, category='content_analysis', conversation_id='', context=None, reply_to='', new_task=False, surface=None, event_type=None, route_id='', session_snapshot_id=''):
        payload = dict(source_id=self.source, account_id=str(account_id), message_id=message_id,
            category=category, conversation_id=conversation_id, content={'text': text, 'images': images or []}, context=compact_context(context or {}), reply_to=reply_to, new_task=new_task)
        if surface is not None or event_type is not None:
            payload.update(surface=surface or 'manual', event_type=event_type or 'manual.request')
        if route_id:
            payload['route_id'] = route_id
        if session_snapshot_id:
            payload['session_snapshot_id'] = session_snapshot_id
        return self.request('POST', '/v1/messages', payload)

    def catalog(self):
        return self.request('GET', '/v1/catalog')

    def status(self, event_id, account_id):
        return self.request('GET', '/v1/messages/' + urllib.parse.quote(event_id, safe='') + '?' + urllib.parse.urlencode({'account_id': str(account_id)}))

    def retry(self, event_id, account_id):
        return self.request('POST', '/v1/messages/' + urllib.parse.quote(event_id, safe='') + '/retry', {'account_id': str(account_id)})

    def upload(self, account_id, blob, filename='image.png'):
        if not isinstance(blob, bytes) or len(blob) > 8 * 1024 * 1024:
            raise ChannelError('图片最多 8MB')
        boundary = 'channel-' + secrets.token_hex(20)
        filename = ''.join(c for c in filename if c.isalnum() or c in '._-')[:100] or 'image.png'
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="account_id"\r\n\r\n{account_id}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + blob + f'\r\n--{boundary}--\r\n'.encode()
        return self.request('POST', '/v1/assets', body=body, content_type='multipart/form-data; boundary=' + boundary)

    def results(self, account_id, after=0):
        return self.request('GET', '/v1/results?' + urllib.parse.urlencode({'account_id': str(account_id), 'after': after}))

    def acknowledge(self, result_id):
        return self.request('POST', '/v1/results/' + urllib.parse.quote(result_id, safe='') + '/ack', {})

    def event(self, account_id, event_id, event_type, text='', conversation_id='', images=None, resource=None, context=None, transport='poll', direction='incoming'):
        return self.request('POST', '/v1/events', dict(account_id=str(account_id), event_id=event_id,
            event_type=event_type, conversation_id=conversation_id, content={'text': text, 'images': images or []},
            resource=resource or {}, context=compact_context(context or {}), transport=transport, direction=direction))

    def heartbeat(self, account_id, executor_id, capabilities):
        return self.request('POST', '/v1/connectors/heartbeat', dict(account_id=str(account_id), executor_id=executor_id, capabilities=capabilities))

    def claim_action(self, account_id, executor_id):
        return self.request('POST', '/v1/actions/claim', dict(account_id=str(account_id), executor_id=executor_id))['action']

    def action_receipt(self, account_id, action, status, external_id='', detail=''):
        return self.request('POST', '/v1/actions/' + urllib.parse.quote(action['id'], safe='') + '/receipt',
            dict(account_id=str(account_id), claim_token=action['claim_token'], status=status, external_id=external_id, detail=detail))


    def session(self, account_id, conversation_id, surface='dm'):
        return self.request('POST', '/v1/sessions', dict(account_id=str(account_id), conversation_id=conversation_id, surface=surface))

    def sync_session(self, account_id, session_id, messages, coverage=None):
        data = dict(account_id=str(account_id), messages=messages)
        if coverage is not None:
            data['coverage'] = coverage
        return self.request('POST', '/v1/sessions/' + urllib.parse.quote(session_id, safe='') + '/messages', data)

    def session_messages(self, account_id, session_id, **filters):
        query = urllib.parse.urlencode(dict(account_id=str(account_id), **filters))
        return self.request('GET', '/v1/sessions/' + urllib.parse.quote(session_id, safe='') + '/messages?' + query)

    def session_snapshot(self, account_id, session_id, request_id, trigger_message_ids, **options):
        return self.request('POST', '/v1/sessions/' + urllib.parse.quote(session_id, safe='') + '/snapshots',
            dict(account_id=str(account_id), request_id=request_id, trigger_message_ids=trigger_message_ids, **options))

    def validate_session_snapshot(self, account_id, snapshot_id):
        return self.request('GET', '/v1/session-snapshots/' + urllib.parse.quote(snapshot_id, safe='') + '/validate?' + urllib.parse.urlencode({'account_id': str(account_id)}))

    def session_memories(self, account_id, session_id):
        return self.request('GET', '/v1/sessions/' + urllib.parse.quote(session_id, safe='') + '/memories?' + urllib.parse.urlencode({'account_id': str(account_id)}))

    def put_session_memory(self, account_id, session_id, key, text, kind, evidence, expected_version=0, ttl_seconds=86400):
        return self.request('POST', '/v1/sessions/' + urllib.parse.quote(session_id, safe='') + '/memories',
            dict(account_id=str(account_id), key=key, text=text, kind=kind, evidence=evidence, expected_version=expected_version, ttl_seconds=ttl_seconds))


    def sessions(self, account_id, **filters):
        return self.request('GET', '/v1/sessions?' + urllib.parse.urlencode(dict(account_id=str(account_id), **filters)))

    def get_session_snapshot(self, account_id, snapshot_id):
        return self.request('GET', '/v1/session-snapshots/' + urllib.parse.quote(snapshot_id, safe='') + '?' + urllib.parse.urlencode({'account_id': str(account_id)}))

    def retire_session_memory(self, account_id, session_id, key, expected_version):
        return self.request('POST', '/v1/sessions/' + urllib.parse.quote(session_id, safe='') + '/memories/' + urllib.parse.quote(key, safe='') + '/retire',
            dict(account_id=str(account_id), expected_version=expected_version))
