"""Ops Hub is the identity authority; local users only anchor business foreign keys."""
import os
import json
import secrets
import urllib.request
import urllib.error
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from backend.app.models import User

def endpoint(): return os.environ.get('OPS_IDENTITY_URL', '').rstrip('/')

def validate(token, cookie=''):
    headers = {'Authorization': 'Bearer ' + token}
    if cookie: headers['Cookie'] = 'ops_workspace_session=' + cookie
    request = urllib.request.Request(endpoint() + '/api/auth/validate?app=xhs-all-in-one', headers=headers)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
            identity = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 403: raise HTTPException(403, '尚未开通小红书工作台，请到统一账号中心申请权限。') from None
        if exc.code == 401: raise HTTPException(401, 'Unified session expired') from None
        raise HTTPException(503, 'Identity service unavailable') from None
    except Exception: raise HTTPException(503, 'Identity service unavailable') from None
    if type(identity.get('id')) is not int or identity['id'] < 1 or not isinstance(identity.get('username'), str):
        raise HTTPException(503, 'Invalid identity response')
    return identity

def local_user(db, identity):
    user = db.get(User, identity['id'])
    if user is None:
        user = User(id=identity['id'], username=identity['username'], password_hash='external-only:' + secrets.token_hex(24), auth_version=identity['auth_version'])
        db.add(user)
        try: db.commit()
        except IntegrityError:
            db.rollback()
            user = db.get(User, identity['id'])
            if user is None: raise HTTPException(409, 'Identity mapping conflict') from None
    if user.username != identity['username']:
        raise HTTPException(409, 'Identity mapping conflict')
    return user
