from __future__ import annotations

from typing import Optional
from datetime import timedelta

from pydantic import BaseModel, Field, field_validator
from fastapi import APIRouter, Depends, HTTPException, status, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user, MEDIA_COOKIE
from backend.app.core.security import create_access_token, create_refresh_token, decode_token, hash_password, verify_password, token_matches_user, ACCESS_TOKEN_EXPIRE_MINUTES, _create_token
from backend.app.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


class AuthCredentials(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=6, max_length=128)

    @field_validator('username', mode='before')
    @classmethod
    def normalize_username(cls, value):
        return value.strip() if isinstance(value, str) else value


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class UserResponse(BaseModel):
    id: int
    username: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    user: Optional[UserResponse] = None


def _serialize_user(user: User) -> dict:
    return {"id": user.id, "username": user.username}


WORKSPACE_COOKIE = 'ops_workspace_session'


def workspace_cookie(response, user):
    token = _create_token(user.id, timedelta(hours=12), 'workspace', user.auth_version)
    response.set_cookie(WORKSPACE_COOKIE, token, max_age=43200, httponly=True,
                        secure=True, samesite='strict', path='/')
    response.headers['Cache-Control'] = 'private, no-store'


def media_cookie(response, request, token):
    response.set_cookie(MEDIA_COOKIE, token, max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
                        httponly=True, secure=request.url.scheme == 'https', samesite='strict', path='/api/files/media')
    response.headers['Cache-Control'] = 'private, no-store'


def _token_response(user: User, response: Response, request: Request) -> dict:
    access = create_access_token(user.id, user.auth_version)
    media_cookie(response, request, access)
    workspace_cookie(response, user)
    return {
        "access_token": access,
        "refresh_token": create_refresh_token(user.id, user.auth_version),
        "token_type": "bearer",
        "user": _serialize_user(user),
    }


@router.post("/register")
def register(credentials: AuthCredentials, response: Response, request: Request, db: Session = Depends(get_db)):
    username = credentials.username.strip()
    existing_user = db.scalar(select(User).where(User.username == username))
    if existing_user is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username already exists")

    user = User(username=username, password_hash=hash_password(credentials.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail='Username already exists') from None
    db.refresh(user)
    return _token_response(user, response, request)


@router.post("/login")
def login(credentials: AuthCredentials, response: Response, request: Request, db: Session = Depends(get_db)):
    username = credentials.username.strip()
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not verify_password(credentials.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    return _token_response(user, response, request)


@router.post("/refresh")
def refresh_token(payload: RefreshRequest, response: Response, request: Request, db: Session = Depends(get_db)):
    decoded = decode_token(payload.refresh_token)
    workspace_token = request.cookies.get(WORKSPACE_COOKIE)
    if not workspace_token and request.headers.get('origin') in {'https://d.58nft.net:9450', 'https://d.58nft.net:9461'}:
        raise HTTPException(401, 'Workspace authentication required')
    if workspace_token:
        workspace = decode_token(workspace_token)
        if workspace.get('token_type') != 'workspace' or workspace.get('user_id') != decoded.get('user_id'):
            raise HTTPException(401, 'Workspace user changed')
    if decoded.get("token_type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    user = db.get(User, decoded["user_id"])
    if not token_matches_user(decoded, user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    access = create_access_token(user.id, user.auth_version)
    media_cookie(response, request, access)
    workspace_cookie(response, user)
    return {"access_token": access, "token_type": "bearer"}


@router.post("/logout")
def logout(response: Response, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.execute(update(User).where(User.id == current_user.id).values(auth_version=User.auth_version + 1))
    db.commit()
    response.delete_cookie(WORKSPACE_COOKIE, path='/', secure=True, httponly=True, samesite='strict')
    response.delete_cookie(MEDIA_COOKIE, path='/api/files/media', httponly=True, samesite='strict')
    return {"status": "ok"}


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return _serialize_user(current_user)


@router.post('/workspace/session')
def workspace_session(response: Response, current_user: User = Depends(get_current_user)):
    workspace_cookie(response, current_user)
    return {'id': current_user.id, 'username': current_user.username}


def workspace_identity(request: Request, db: Session):
    authorization = request.headers.get('authorization', '')
    if not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Workspace authentication required')
    payload = decode_token(authorization[7:])
    if payload.get('token_type') != 'workspace':
        raise HTTPException(401, 'Invalid workspace token')
    user = db.get(User, payload['user_id'])
    if not token_matches_user(payload, user):
        raise HTTPException(401, 'Workspace session expired')
    return user


@router.get('/workspace/verify')
def verify_workspace(request: Request, response: Response, db: Session = Depends(get_db)):
    user = workspace_identity(request, db)
    response.headers['Cache-Control'] = 'private, no-store'
    return _serialize_user(user)


@router.post('/workspace/logout')
def logout_workspace(request: Request, response: Response, db: Session = Depends(get_db)):
    user = workspace_identity(request, db)
    return logout(response, user, db)


@router.post('/workspace/exchange')
def exchange_workspace(request: Request, response: Response, db: Session = Depends(get_db)):
    # Cookie authentication requires an explicit same-origin browser request.
    allowed = {'https://d.58nft.net:9450', 'https://d.58nft.net:9461'}
    if request.headers.get('origin') not in allowed or request.headers.get('x-app-request') != '1':
        raise HTTPException(403, 'Invalid workspace origin')
    token = request.cookies.get(WORKSPACE_COOKIE)
    if not token:
        raise HTTPException(401, 'Workspace authentication required')
    payload = decode_token(token)
    if payload.get('token_type') != 'workspace':
        raise HTTPException(401, 'Invalid workspace token')
    user = db.get(User, payload['user_id'])
    if not token_matches_user(payload, user):
        raise HTTPException(401, 'Workspace session expired')
    return _token_response(user, response, request)
