from __future__ import annotations

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.security import decode_token, token_matches_user
from backend.app.models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)
MEDIA_COOKIE = 'xhs_private_media'


def get_current_user(request: Request, token: str | None = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    if not token and request.method in ('GET', 'HEAD') and request.url.path.startswith('/api/files/media/'):
        token = request.cookies.get(MEDIA_COOKIE)
    if not token:
        raise HTTPException(401, 'Not authenticated', headers={'WWW-Authenticate': 'Bearer'})
    from .ops_identity import endpoint, validate, local_user
    if endpoint():
        return local_user(db, validate(token, request.cookies.get('ops_workspace_session', '')))
    payload = decode_token(token)
    if payload.get("token_type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    workspace_token = request.cookies.get('ops_workspace_session')
    if workspace_token:
        workspace = decode_token(workspace_token)
        if workspace.get('token_type') != 'workspace' or workspace.get('user_id') != payload['user_id']:
            raise HTTPException(401, 'Workspace user changed')
    user = db.get(User, payload["user_id"])
    if not token_matches_user(payload, user):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
