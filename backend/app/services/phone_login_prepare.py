"""Bounded, owner-scoped PC login preparations; preparing never sends SMS."""
import secrets
import threading
import time
from dataclasses import dataclass

TTL = 180
MAX_ENTRIES = 8
_lock = threading.Lock()
_slots = threading.BoundedSemaphore(MAX_ENTRIES)
_entries = {}


@dataclass
class Prepared:
    owner_id: int
    account_id: int | None
    api: object
    cookies: dict
    expires_at: float
    timings: dict


def _discard(token):
    entry = _entries.pop(token, None)
    if entry:
        try:
            entry.api.close()
        finally:
            _slots.release()


def _expire():
    for token, entry in list(_entries.items()):
        if entry.expires_at <= time.monotonic():
            _discard(token)


def prepare(owner_id, account_id, factory):
    with _lock:
        _expire()
        for token, entry in _entries.items():
            if (entry.owner_id, entry.account_id) == (owner_id, account_id):
                return token, max(1, int(entry.expires_at - time.monotonic()))
        if not _slots.acquire(blocking=False):
            raise ValueError('登录准备繁忙，请稍后重试')
    api = None
    try:
        api, cookies, timings = factory()
        token = secrets.token_urlsafe(32)
        with _lock:
            # A second tab may have completed a preparation in the meantime.
            for existing, entry in _entries.items():
                if (entry.owner_id, entry.account_id) == (owner_id, account_id):
                    api.close()
                    _slots.release()
                    return existing, max(1, int(entry.expires_at - time.monotonic()))
            _entries[token] = Prepared(owner_id, account_id, api, cookies, time.monotonic() + TTL, timings)
        timer = threading.Timer(TTL + 1, expire)
        timer.daemon = True
        timer.start()
        return token, TTL
    except BaseException:
        if api:
            api.close()
        _slots.release()
        raise


def expire():
    with _lock:
        _expire()


def take(token, owner_id, account_id):
    with _lock:
        _expire()
        entry = _entries.get(token)
        if entry is None or (entry.owner_id, entry.account_id) != (owner_id, account_id):
            raise ValueError('登录准备已过期或不属于当前账号，请重新打开登录窗口')
        del _entries[token]
        _slots.release()
        return entry
