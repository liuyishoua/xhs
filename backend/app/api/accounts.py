from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.adapters.xhs.creator_login_adapter import XhsCreatorLoginAdapter
from backend.app.adapters.xhs.pc_api_adapter import XhsPcApiAdapter
from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.security import decrypt_text
from backend.app.core.time import shanghai_now
from backend.app.models import AccountCookieVersion, PlatformAccount, User
from backend.app.schemas.common import paginated
from backend.app.services.account_service import (
    account_profile_from_user_info,
    cookie_header_from_text,
    decode_cookie_text,
    enrich_user_info_with_xhs_self_profile,
    serialize_account,
    require_relogin_account,
    upsert_platform_account_from_login,
    persist_refreshed_account_cookies,
    record_session_event,
    cookie_version_is_current,
)
from backend.app.services.account_health import AccountCheckError
from xhs_utils.cookie_util import trans_cookies

router = APIRouter(prefix="/accounts", tags=["accounts"])


class CookieImportRequest(BaseModel):
    platform: str = Field(pattern="^xhs$")
    sub_type: str = Field(pattern="^(pc|creator)$")
    cookie_string: str = Field(min_length=3)
    sync_creator: bool = False
    account_id: int | None = Field(default=None, gt=0)


def get_pc_account_adapter() -> XhsPcLoginAdapter:
    return XhsPcLoginAdapter()


def get_creator_account_adapter() -> XhsCreatorLoginAdapter:
    return XhsCreatorLoginAdapter()


class XhsSelfProfileAdapter:
    def get_self_profile(self, cookies_text: str):
        return XhsPcApiAdapter(cookies_text).get_self_info()


def get_xhs_self_profile_adapter() -> XhsSelfProfileAdapter:
    return XhsSelfProfileAdapter()


def _select_adapter(sub_type: str, pc_adapter: XhsPcLoginAdapter, creator_adapter: XhsCreatorLoginAdapter):
    return creator_adapter if sub_type == "creator" else pc_adapter


def _sync_creator_account_from_pc_cookie(
    *,
    db: Session,
    user_id: int,
    platform: str,
    cookie_string: str,
    creator_adapter: XhsCreatorLoginAdapter,
):
    try:
        creator_result = creator_adapter.exchange_from_user_cookies(trans_cookies(cookie_string))
        creator_cookies_text = json.dumps(creator_result["cookies"], ensure_ascii=False, separators=(",", ":"))
        creator_user_info = creator_adapter.get_user_info(creator_result["cookies"])
        upsert_platform_account_from_login(
            db=db,
            user_id=user_id,
            platform=platform,
            sub_type="creator",
            user_info=creator_user_info,
            cookies_text=creator_cookies_text,
        )
    except Exception:
        return None
    return True


@router.get("")
def get_accounts(
    platform: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = select(PlatformAccount).where(PlatformAccount.user_id == current_user.id)
    if platform:
        statement = statement.where(PlatformAccount.platform == platform)
    accounts = db.scalars(statement.order_by(PlatformAccount.created_at.desc())).all()
    return paginated(
        [serialize_account(account) for account in accounts],
        page,
        page_size,
    )


@router.post("/import-cookie")
def import_cookie(
    payload: CookieImportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    pc_adapter: XhsPcLoginAdapter = Depends(get_pc_account_adapter),
    creator_adapter: XhsCreatorLoginAdapter = Depends(get_creator_account_adapter),
    self_profile_adapter: XhsSelfProfileAdapter = Depends(get_xhs_self_profile_adapter),
):
    require_relogin_account(db, current_user.id, payload.platform, payload.sub_type, payload.account_id)
    adapter = _select_adapter(payload.sub_type, pc_adapter, creator_adapter)
    imported_cookies = trans_cookies(payload.cookie_string)
    checked_cookies = dict(imported_cookies)
    try:
        user_info = adapter.get_user_info(checked_cookies)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cookie is invalid or expired") from exc

    if payload.sub_type == "pc":
        try:
            self_profile = self_profile_adapter.get_self_profile(cookie_header_from_text(payload.cookie_string))
            user_info = enrich_user_info_with_xhs_self_profile(user_info, self_profile)
        except Exception:
            pass

    account, action = upsert_platform_account_from_login(
        db=db,
        user_id=current_user.id,
        platform=payload.platform,
        sub_type=payload.sub_type,
        account_id=payload.account_id,
        user_info=user_info,
        cookies_text=(json.dumps(checked_cookies, ensure_ascii=False, separators=(',', ':'))
                      if payload.sub_type == 'pc' and checked_cookies != imported_cookies else payload.cookie_string),
    )
    if payload.sub_type == "pc" and payload.sync_creator:
        _sync_creator_account_from_pc_cookie(
            db=db,
            user_id=current_user.id,
            platform=payload.platform,
            cookie_string=payload.cookie_string,
            creator_adapter=creator_adapter,
        )
    db.commit()
    db.refresh(account)
    return serialize_account(account, action)


@router.post("/{account_id}/check")
def check_account(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    pc_adapter: XhsPcLoginAdapter = Depends(get_pc_account_adapter),
    creator_adapter: XhsCreatorLoginAdapter = Depends(get_creator_account_adapter),
    self_profile_adapter: XhsSelfProfileAdapter = Depends(get_xhs_self_profile_adapter),
):
    account = db.get(PlatformAccount, account_id)
    if account is None or account.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    cookie_version = db.scalars(
        select(AccountCookieVersion)
        .where(AccountCookieVersion.platform_account_id == account.id)
        .order_by(AccountCookieVersion.created_at.desc())
    ).first()
    if cookie_version is None:
        account.status = "expired"
        account.status_message = "No stored cookie version"
        record_session_event(db, account, 'manual_check', 'expired')
        db.commit()
        db.refresh(account)
        return serialize_account(account)

    adapter = _select_adapter(account.sub_type or "pc", pc_adapter, creator_adapter)
    check_code = None
    try:
        cookies_text = decrypt_text(cookie_version.encrypted_cookies)
        original_cookies = decode_cookie_text(cookies_text)
        checked_cookies = dict(original_cookies)
        user_info = (adapter.check_session(checked_cookies) if account.sub_type == 'creator'
                     else adapter.get_user_info(checked_cookies))
        if user_info.get('external_user_id') != account.external_user_id:
            raise AccountCheckError('risk', '登录 Cookie 与绑定账号不一致，请重新绑定所选账号')
        if not persist_refreshed_account_cookies(
                db, account.id, original_cookies, checked_cookies, source='manual_check'):
            raise HTTPException(409, '登录凭证已更新，请重新检查')
        try:
            if account.sub_type == 'pc':
                self_profile = self_profile_adapter.get_self_profile(
                    cookie_header_from_text(json.dumps(checked_cookies)))
                user_info = enrich_user_info_with_xhs_self_profile(user_info, self_profile)
        except Exception:
            pass
        account.status = "active"
        account.status_message = ""
        account.nickname = user_info.get("nickname", account.nickname)
        account.avatar_url = user_info.get("avatar_url", account.avatar_url)
        account.external_user_id = user_info.get("external_user_id", account.external_user_id)
        account.profile_json = json.dumps(account_profile_from_user_info(user_info), ensure_ascii=False, separators=(",", ":"))
        account.updated_at = shanghai_now()

    except HTTPException:
        raise
    except Exception as exc:
        if not cookie_version_is_current(db, account.id, cookie_version.id, cookie_version.encrypted_cookies):
            raise HTTPException(409, '登录凭证已更新，请重新检查') from None
        check_code = getattr(exc, 'business_code', None)
        account.status = exc.account_status if isinstance(exc, AccountCheckError) else 'unknown'
        account.status_message = str(exc) if isinstance(exc, AccountCheckError) else '账号检查暂未完成，请检查家庭网络后重试'
        account.updated_at = shanghai_now()

    record_session_event(db, account, 'manual_check', account.status, cookie_version_id=cookie_version.id,
                         business_code=check_code)
    db.commit()
    db.refresh(account)
    return serialize_account(account)


@router.patch("/{account_id}")
def update_account(account_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    account = db.get(PlatformAccount, account_id)
    if account is None or account.user_id != current_user.id:
        raise HTTPException(404, 'Account not found')
    raise HTTPException(405, '请通过重新登录或检查更新账号资料')


@router.delete("/{account_id}")
def delete_account(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    account = db.get(PlatformAccount, account_id)
    if account is None or account.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    from backend.app.models.direct_message import DmListener, DirectMessage, DmHistory, DmConversation, DmManualMessage, DmMedia
    from sqlalchemy import delete, update
    from backend.app.core.time import shanghai_now
    db.execute(update(PlatformAccount).where(PlatformAccount.id == account_id).values(updated_at=shanghai_now()))
    listener = db.get(DmListener, account_id)
    if listener and listener.enabled:
        raise HTTPException(409, '请先停止该账号的私信监听再删除账号')
    if db.scalar(select(DirectMessage.id).where(DirectMessage.account_id == account_id, DirectMessage.state == 'sending')):
        raise HTTPException(409, '该账号正在发送私信，请等待发送结束')
    if db.scalar(select(DmManualMessage.id).where(DmManualMessage.account_id == account_id, DmManualMessage.state == 'sending')):
        raise HTTPException(409, '该账号正在发送私信，请等待发送结束')
    db.execute(delete(DmManualMessage).where(DmManualMessage.account_id == account_id))
    db.execute(delete(DmMedia).where(DmMedia.account_id == account_id))
    db.execute(delete(DirectMessage).where(DirectMessage.account_id == account_id))
    db.execute(delete(DmHistory).where(DmHistory.account_id == account_id))
    db.execute(delete(DmConversation).where(DmConversation.account_id == account_id))
    db.execute(delete(DmListener).where(DmListener.account_id == account_id))
    for cookie_version in db.scalars(
        select(AccountCookieVersion).where(AccountCookieVersion.platform_account_id == account.id)
    ).all():
        db.delete(cookie_version)
    db.delete(account)
    db.commit()
    return {"id": account_id, "status": "deleted"}
