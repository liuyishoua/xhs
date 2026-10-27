from __future__ import annotations

import base64
import io
import json

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.adapters.xhs.creator_login_adapter import XhsCreatorLoginAdapter
from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter
from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.core.security import decrypt_text, encrypt_text
from backend.app.models import LoginSession, PlatformAccount, User, ApiLog
from backend.app.services.login_errors import login_failure_detail
from backend.app.adapters.xhs.pc_api_adapter import XhsPcApiAdapter
from backend.app.services.account_service import (
    cookie_header_from_text,
    enrich_user_info_with_xhs_self_profile,
    serialize_account,
    require_relogin_account,
    upsert_platform_account_from_login,
)

router = APIRouter(prefix="/xhs/login-sessions", tags=["xhs-login-sessions"])


class PcQrCodeRequest(BaseModel):
    sync_creator: bool = False
    account_id: int | None = Field(default=None, gt=0)


class PhonePrepareRequest(BaseModel):
    account_id: int | None = Field(default=None, gt=0)


class PhoneSendCodeRequest(BaseModel):
    preparation_token: str | None = Field(default=None, min_length=20, max_length=128)
    phone: str = Field(min_length=6, max_length=32)
    sync_creator: bool = False
    account_id: int | None = Field(default=None, gt=0)


class PhoneConfirmRequest(BaseModel):
    session_id: int
    phone: str = Field(min_length=6, max_length=32)
    code: str = Field(min_length=4, max_length=12)
    sync_creator: bool | None = None


def _dump_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: str | None) -> dict:
    if not value:
        return {}
    return json.loads(value)


def _dump_temp_state(cookies: dict, *, sync_creator: bool = False, account_id: int | None = None, pc_login_state: dict | None = None) -> str:
    if sync_creator or account_id is not None or pc_login_state is not None:
        payload = {"cookies": cookies, "sync_creator": sync_creator}
        if pc_login_state is not None:
            payload["pc_login_state"] = pc_login_state
        if account_id is not None:
            payload["account_id"] = account_id
        return _dump_json(payload)
    return _dump_json(cookies)


def _load_temp_state(value: str | None) -> tuple[dict, bool, int | None]:
    payload = _load_json(value)
    if isinstance(payload, dict) and isinstance(payload.get("cookies"), dict):
        return payload["cookies"], bool(payload.get("sync_creator")), payload.get("account_id")
    if isinstance(payload, dict):
        return payload, False, None
    return {}, False, None


def _qr_data_url(qr_url: str) -> str:
    import qrcode

    image = qrcode.make(qr_url)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def get_pc_login_adapter() -> XhsPcLoginAdapter:
    return XhsPcLoginAdapter()


def get_creator_login_adapter() -> XhsCreatorLoginAdapter:
    return XhsCreatorLoginAdapter()


def _mask_phone(phone: str) -> str:
    if len(phone) <= 7:
        return f"{phone[:2]}****"
    return f"{phone[:3]}****{phone[-4:]}"


def _sync_creator_account_from_pc_login(
    *,
    db: Session,
    user_id: int,
    pc_cookies: dict,
    creator_adapter: XhsCreatorLoginAdapter,
) -> dict | None:
    try:
        creator_result = creator_adapter.exchange_from_user_cookies(pc_cookies)
        creator_user_info = creator_adapter.get_user_info(creator_result["cookies"])
        creator_account, creator_action = _create_account_from_login(
            db=db,
            user_id=user_id,
            sub_type="creator",
            user_info=creator_user_info,
            cookies=creator_result["cookies"],
        )
    except Exception:
        return None
    return serialize_account(creator_account, creator_action)


def _create_account_from_login(
    *,
    db: Session,
    user_id: int,
    sub_type: str,
    user_info: dict,
    cookies: dict,
    account_id: int | None = None,
) -> tuple[PlatformAccount, str]:
    cookies_text = _dump_json(cookies)
    if sub_type == "pc":
        try:
            self_profile = XhsPcApiAdapter(cookie_header_from_text(cookies_text)).get_self_info()
            user_info = enrich_user_info_with_xhs_self_profile(user_info, self_profile)
        except Exception:
            pass
    return upsert_platform_account_from_login(
        db=db,
        user_id=user_id,
        platform="xhs",
        sub_type=sub_type,
        user_info=user_info,
        cookies_text=cookies_text,
        account_id=account_id,
    )


@router.post("/pc/qrcode")
def pc_qrcode(
    payload: PcQrCodeRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter: XhsPcLoginAdapter = Depends(get_pc_login_adapter),
):
    payload = payload or PcQrCodeRequest()
    require_relogin_account(db, current_user.id, "xhs", "pc", payload.account_id)
    try:
        qr_payload = adapter.create_qrcode()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=login_failure_detail(exc, "获取登录二维码失败"),
        ) from exc
    session = LoginSession(
        user_id=current_user.id,
        platform="xhs",
        sub_type="pc",
        status="pending",
        qr_id=qr_payload["qr_id"],
        code=qr_payload["code"],
        qr_url=qr_payload["qr_url"],
        encrypted_temp_cookies=encrypt_text(
            _dump_temp_state(qr_payload["cookies"], sync_creator=payload.sync_creator, account_id=payload.account_id)
        ),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return {
        "session_id": session.id,
        "status": session.status,
        "qr_url": session.qr_url,
        "qr_image_data_url": _qr_data_url(session.qr_url or ""),
    }


@router.post("/creator/qrcode")
def creator_qrcode(
    payload: PcQrCodeRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter: XhsCreatorLoginAdapter = Depends(get_creator_login_adapter),
):
    request = payload or PcQrCodeRequest()
    require_relogin_account(db, current_user.id, "xhs", "creator", request.account_id)
    try:
        payload = adapter.create_qrcode()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=login_failure_detail(exc, "生成 Creator 二维码失败"),
        ) from exc
    session = LoginSession(
        user_id=current_user.id,
        platform="xhs",
        sub_type="creator",
        status="pending",
        qr_id=payload["qr_id"],
        qr_url=payload["qr_url"],
        encrypted_temp_cookies=encrypt_text(_dump_temp_state(payload["cookies"], account_id=request.account_id)),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return {
        "session_id": session.id,
        "status": session.status,
        "qr_url": session.qr_url,
        "qr_image_data_url": _qr_data_url(session.qr_url or ""),
    }


@router.get("/{session_id}")
def login_session(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    pc_adapter: XhsPcLoginAdapter = Depends(get_pc_login_adapter),
    creator_adapter: XhsCreatorLoginAdapter = Depends(get_creator_login_adapter),
):
    session = db.get(LoginSession, session_id)
    if session is None or session.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Login session not found")
    if session.status in {"confirmed", "expired"}:
        return {"session_id": session.id, "status": session.status, "qr_url": session.qr_url}
    if session.sub_type not in {"pc", "creator"} or not session.qr_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported login session")

    cookies, sync_creator, account_id = _load_temp_state(decrypt_text(session.encrypted_temp_cookies))
    require_relogin_account(db, current_user.id, "xhs", session.sub_type, account_id)
    if session.sub_type == "pc":
        if not session.code:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported login session")
        try:
            result = pc_adapter.check_qrcode_status(session.qr_id, session.code, cookies)
        except Exception as exc:
            raise HTTPException(502, login_failure_detail(exc, "扫码登录未完成")) from None
        account_sub_type = "pc"
        user_info = pc_adapter.get_user_info(result["cookies"]) if result["status"] == "confirmed" else None
    else:
        result = creator_adapter.check_qrcode_status(session.qr_id, cookies)
        account_sub_type = "creator"
        user_info = creator_adapter.get_user_info(result["cookies"]) if result["status"] == "confirmed" else None
    session.status = result["status"]
    session.encrypted_temp_cookies = encrypt_text(
        _dump_temp_state(result["cookies"], sync_creator=sync_creator, account_id=account_id)
    )

    account_payload = None
    creator_account_payload = None
    if session.status == "confirmed":
        account, action = _create_account_from_login(
            db=db,
            user_id=current_user.id,
            sub_type=account_sub_type,
            account_id=account_id,
            user_info=user_info,
            cookies=result["cookies"],
        )
        account_payload = serialize_account(account, action)
        if account_sub_type == "pc" and sync_creator:
            creator_account_payload = _sync_creator_account_from_pc_login(
                db=db,
                user_id=current_user.id,
                pc_cookies=result["cookies"],
                creator_adapter=creator_adapter,
            )

    db.commit()
    return {
        "session_id": session.id,
        "status": session.status,
        "qr_url": session.qr_url,
        "account": account_payload,
        "creator_account": creator_account_payload,
    }


@router.post("/pc/phone/prepare")
def pc_phone_prepare(
    payload: PhonePrepareRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter: XhsPcLoginAdapter = Depends(get_pc_login_adapter),
):
    from backend.app.services.phone_login_prepare import prepare
    require_relogin_account(db, current_user.id, "xhs", "pc", payload.account_id)
    try:
        token, expires_in = prepare(current_user.id, payload.account_id, adapter.prepare_phone_session)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=login_failure_detail(exc, "登录准备失败")) from exc
    return {"preparation_token": token, "expires_in": expires_in}


@router.post("/pc/phone/send-code")
def pc_phone_send_code(
    payload: PhoneSendCodeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter: XhsPcLoginAdapter = Depends(get_pc_login_adapter),
):
    require_relogin_account(db, current_user.id, "xhs", "pc", payload.account_id)
    try:
        if payload.preparation_token:
            from backend.app.services.phone_login_prepare import take
            try:
                prepared = take(payload.preparation_token, current_user.id, payload.account_id)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            result = adapter.send_prepared_phone_code(payload.phone, prepared)
        else:
            result = adapter.create_phone_session(payload.phone)
    except HTTPException:
        raise
    except Exception as exc:
        db.add(ApiLog(user_id=current_user.id, platform="xhs", endpoint="internal/xhs/pc/phone/send-code", status="failed",
                      meta={**getattr(adapter, "last_phone_diagnostics", {}), "error_type": type(exc).__name__}))
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=login_failure_detail(exc, "发送验证码失败")) from exc
    session = LoginSession(
        user_id=current_user.id,
        platform="xhs",
        sub_type="pc",
        login_method="phone",
        phone_mask=_mask_phone(payload.phone),
        status="pending",
        encrypted_temp_cookies=encrypt_text(
            _dump_temp_state(result["cookies"], sync_creator=payload.sync_creator, account_id=payload.account_id,
                             pc_login_state=result.get("pc_login_state"))
        ),
    )
    db.add(session)
    db.add(ApiLog(user_id=current_user.id, platform="xhs", endpoint="internal/xhs/pc/phone/send-code",
                  status="platform_accepted", meta=getattr(adapter, "last_phone_diagnostics", {})))
    db.commit()
    db.refresh(session)
    from backend.app.services.sms_relay import register_pending
    register_pending(current_user.id, session.id, payload.phone, payload.account_id, db=db)
    return {"session_id": session.id, "status": session.status, "message": result.get("message", "sent")}


@router.post("/pc/phone/confirm")
def pc_phone_confirm(
    payload: PhoneConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter: XhsPcLoginAdapter = Depends(get_pc_login_adapter),
    creator_adapter: XhsCreatorLoginAdapter = Depends(get_creator_login_adapter),
):
    return _confirm_phone_login(payload, current_user, db, adapter, "pc", creator_adapter)


@router.post("/creator/phone/send-code")
def creator_phone_send_code(
    payload: PhoneSendCodeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter: XhsCreatorLoginAdapter = Depends(get_creator_login_adapter),
):
    require_relogin_account(db, current_user.id, "xhs", "creator", payload.account_id)
    try:
        result = adapter.create_phone_session(payload.phone)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=login_failure_detail(exc, "发送验证码失败")) from exc
    session = LoginSession(
        user_id=current_user.id,
        platform="xhs",
        sub_type="creator",
        login_method="phone",
        phone_mask=_mask_phone(payload.phone),
        status="pending",
        encrypted_temp_cookies=encrypt_text(_dump_temp_state(result["cookies"], account_id=payload.account_id)),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    from backend.app.services.sms_relay import register_pending
    register_pending(current_user.id, session.id, payload.phone, payload.account_id, db=db)
    return {"session_id": session.id, "status": session.status, "message": result.get("message", "sent")}


@router.post("/creator/phone/confirm")
def creator_phone_confirm(
    payload: PhoneConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    adapter: XhsCreatorLoginAdapter = Depends(get_creator_login_adapter),
):
    return _confirm_phone_login(payload, current_user, db, adapter, "creator", None)


def _confirm_phone_login(
    payload: PhoneConfirmRequest,
    current_user: User,
    db: Session,
    adapter,
    sub_type: str,
    creator_adapter: XhsCreatorLoginAdapter | None,
):
    session = db.get(LoginSession, payload.session_id)
    if (
        session is None
        or session.user_id != current_user.id
        or session.sub_type != sub_type
        or session.login_method != "phone"
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Login session not found")
    if session.status == 'verification_required':
        raise HTTPException(409, '小红书要求安全验证（HTTP 471），本次短信登录已暂停；请在官方网页完成验证后，通过扫码登录或 Cookie 导入接入。')
    cookies, stored_sync_creator, account_id = _load_temp_state(decrypt_text(session.encrypted_temp_cookies))
    require_relogin_account(db, current_user.id, "xhs", sub_type, account_id)
    phase = 'confirming_login'
    try:
        state = _load_json(decrypt_text(session.encrypted_temp_cookies)).get("pc_login_state") if sub_type == "pc" else None
        kwargs = {"pc_login_state": state} if state is not None else {}
        result = adapter.confirm_phone_login(payload.phone, payload.code, cookies, **kwargs)
        phase = 'checking_identity'
        user_info = adapter.get_user_info(result["cookies"])
    except Exception as exc:
        observed = getattr(adapter, 'last_phone_diagnostics', {})
        allowed = ('phase', 'state_restored', 'http_status', 'platform_code', 'platform_success',
                   'code_check_passed', 'has_mobile_token', 'has_session')
        diagnostics = {key: observed[key] for key in allowed if key in observed and isinstance(observed[key], (str, int, bool))}
        if phase == 'checking_identity':
            diagnostics['phase'] = phase
        if getattr(exc, 'http_status', None) == 471:
            session.status = 'verification_required'
        db.add(ApiLog(user_id=current_user.id, platform='xhs', endpoint=f'internal/xhs/{sub_type}/phone/confirm',
                      status='failed', meta={**diagnostics, 'session_id': session.id, 'error_type': type(exc).__name__}))
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=login_failure_detail(exc, "验证码登录失败")) from exc

    sync_creator = payload.sync_creator if payload.sync_creator is not None else stored_sync_creator
    session.status = "confirmed"
    session.encrypted_temp_cookies = encrypt_text(
        _dump_temp_state(result["cookies"], sync_creator=sync_creator, account_id=account_id)
    )
    account, action = _create_account_from_login(
        db=db,
        user_id=current_user.id,
        sub_type=sub_type,
        account_id=account_id,
        user_info=user_info,
        cookies=result["cookies"],
    )
    creator_account_payload = None
    if sub_type == "pc" and creator_adapter is not None and sync_creator:
        creator_account_payload = _sync_creator_account_from_pc_login(
            db=db,
            user_id=current_user.id,
            pc_cookies=result["cookies"],
            creator_adapter=creator_adapter,
        )
    db.add(ApiLog(user_id=current_user.id, platform='xhs', endpoint=f'internal/xhs/{sub_type}/phone/confirm',
                  status='confirmed', meta={'session_id': session.id, 'phase': 'completed', 'account_id': account.id}))
    db.commit()
    return {
        "session_id": session.id,
        "status": session.status,
        "account": serialize_account(account, action),
        "creator_account": creator_account_payload,
    }
