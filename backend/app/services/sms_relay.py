"""Owned-phone SMS relay. Never persist SMS bodies or one-time codes."""
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote_plus
from contextvars import ContextVar

from backend.app.core.security import encrypt_text, decrypt_text

DIRECTORY = Path(__file__).resolve().parents[3] / '.local'
maintenance_managed = ContextVar('maintenance_managed', default=False)


@contextmanager
def connect():
    DIRECTORY.mkdir(mode=0o700, exist_ok=True)
    path = DIRECTORY / 'sms-relay.sqlite3'
    db = sqlite3.connect(path, timeout=10)
    path.chmod(0o600)
    db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE IF NOT EXISTS devices(user_id INTEGER PRIMARY KEY, secret TEXT NOT NULL, last_received REAL, last_state TEXT);
      CREATE TABLE IF NOT EXISTS pending(session_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, phone TEXT NOT NULL,
        expires REAL NOT NULL, state TEXT NOT NULL DEFAULT 'waiting', created REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS receipts(digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL, state TEXT NOT NULL, created REAL NOT NULL);
    ''')
    if 'xhs_senders' not in {r[1] for r in db.execute('PRAGMA table_info(devices)')}:
        try:
            db.execute("ALTER TABLE devices ADD COLUMN xhs_senders TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            if 'xhs_senders' not in {r[1] for r in db.execute('PRAGMA table_info(devices)')}:
                raise
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def settings(user_id):
    with connect() as db:
        row = db.execute('SELECT * FROM devices WHERE user_id=?', (user_id,)).fetchone()
        if row is None:
            db.execute('INSERT OR IGNORE INTO devices(user_id,secret) VALUES(?,?)',
                       (user_id, encrypt_text(secrets.token_urlsafe(32))))
            row = db.execute('SELECT * FROM devices WHERE user_id=?', (user_id,)).fetchone()
        return {'secret': decrypt_text(row['secret']), 'last_received': row['last_received'], 'last_state': row['last_state'],
                'xhs_senders': json.loads(row['xhs_senders'])}


def set_senders(user_id, senders):
    settings(user_id)
    with connect() as db:
        db.execute('UPDATE devices SET xhs_senders=? WHERE user_id=?', (json.dumps(senders), user_id))


def register_pending(user_id, session_id, phone, account_id, *, db=None):
    if maintenance_managed.get():return
    if db is not None:
        from backend.app.core.database import engine
        if db.get_bind() is not engine:return
    # Only explicitly selected owned accounts can be renewed automatically.
    if not account_id:
        return
    with connect() as db:
        if not db.execute('SELECT 1 FROM devices WHERE user_id=?', (user_id,)).fetchone():
            return
        db.execute("UPDATE pending SET state='superseded' WHERE user_id=? AND state='waiting'", (user_id,))
        db.execute('INSERT INTO pending VALUES(?,?,?,?,?,?)',
                   (session_id, user_id, encrypt_text(phone), time.time() + 300, 'waiting', time.time()))


def authenticate(user_id, stamp, sign):
    with connect() as db:
        row = db.execute('SELECT secret FROM devices WHERE user_id=?', (user_id,)).fetchone()
    if not row or not 1 <= len(str(stamp)) <= 16 or not str(stamp).isdigit() or abs(time.time() - int(stamp) / 1000) > 300:
        return False
    secret = decrypt_text(row['secret'])
    expected = base64.b64encode(hmac.new(secret.encode(), (str(stamp)+'\n'+secret).encode(), hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, str(sign)) or hmac.compare_digest(expected, unquote_plus(str(sign)))


def claim(user_id, content, sender=''):
    import re
    with connect() as db:
        device = db.execute('SELECT xhs_senders FROM devices WHERE user_id=?', (user_id,)).fetchone()
    allowed = json.loads(device['xhs_senders']) if device else []
    recognized = '小红书' in content or bool(sender and sender.strip() in allowed)
    if not recognized or '验证码' not in content:
        with connect() as db:
            db.execute("UPDATE devices SET last_received=?,last_state='received_test_or_ignored' WHERE user_id=?", (time.time(), user_id))
        return None, 'received_test_or_ignored'
    # Default templates include dates and SIM metadata. Bind the OTP to its
    # explicit label instead of treating the year as another code.
    codes = set(re.findall(r'(?:验证码|校验码|动态码|验证代码)(?:[为是:：\s]){0,8}(\d{4,8})(?!\d)', content))
    if len(codes) != 1:
        with connect() as db:
            db.execute("UPDATE devices SET last_received=?,last_state='ambiguous_code' WHERE user_id=?", (time.time(), user_id))
        return None, 'ambiguous_code'
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("SELECT * FROM pending WHERE user_id=? AND state='waiting' AND expires>?", (user_id, time.time())).fetchall()
        if len(rows) != 1:
            db.execute("UPDATE devices SET last_received=?,last_state='no_pending_login' WHERE user_id=?", (time.time(), user_id))
            return None, 'no_pending_login'
        pending = dict(rows[0])
        secret = decrypt_text(db.execute('SELECT secret FROM devices WHERE user_id=?', (user_id,)).fetchone()[0])
        digest = hmac.new(secret.encode(), (str(pending['session_id'])+'\n'+content).encode(), hashlib.sha256).hexdigest()
        if db.execute('SELECT 1 FROM receipts WHERE digest=?', (digest,)).fetchone():
            return None, 'duplicate'
        db.execute("INSERT INTO receipts VALUES(?,?,'processing',?)", (digest, user_id, time.time()))
        db.execute("UPDATE pending SET state='processing' WHERE session_id=?", (pending['session_id'],))
        db.execute("UPDATE devices SET last_received=?,last_state='processing' WHERE user_id=?", (time.time(), user_id))
        pending.update(code=codes.pop(), digest=digest, phone=decrypt_text(pending['phone']))
        return pending, 'processing'


def complete(pending, state):
    with connect() as db:
        db.execute('UPDATE pending SET state=? WHERE session_id=?', (state, pending['session_id']))
        db.execute('UPDATE receipts SET state=? WHERE digest=?', (state, pending['digest']))
        db.execute('UPDATE devices SET last_state=? WHERE user_id=?', (state, pending['user_id']))
