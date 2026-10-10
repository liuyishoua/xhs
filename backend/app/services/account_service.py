from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session

from backend.app.core.security import decrypt_text, encrypt_text
from backend.app.core.time import shanghai_now
from backend.app.models import AccountCookieVersion, PlatformAccount
from xhs_utils.cookie_util import trans_cookies


def account_profile_from_user_info(user_info: dict[str, Any]) -> dict[str, Any]:
    profile = user_info.get("profile")
    if isinstance(profile, dict):
        return profile
    return {}


def decode_cookie_text(value: str) -> dict[str, Any]:
    stripped = value.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    return trans_cookies(stripped)


def cookie_header_from_text(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return ""
    if not stripped.startswith("{"):
        return stripped
    cookies = decode_cookie_text(stripped)
    return "; ".join(f"{key}={cookie_value}" for key, cookie_value in cookies.items())


def persist_refreshed_pc_cookies(db, account_id, original, refreshed):
    """Update the current encrypted version only if no other writer replaced it.

    A stale IM connection or check must never overwrite a new QR login. Updating
    in place bounds storage; version creation still represents explicit logins.
    The caller must validate the platform response and account identity first.
    """
    row = db.scalar(select(AccountCookieVersion).where(
        AccountCookieVersion.platform_account_id == account_id
    ).order_by(AccountCookieVersion.id.desc()))
    if row is None or decode_cookie_text(decrypt_text(row.encrypted_cookies)) != original:
        return False
    if refreshed == original:
        return True
    latest_id = select(func.max(AccountCookieVersion.id)).where(
        AccountCookieVersion.platform_account_id == account_id).scalar_subquery()
    result = db.execute(update(AccountCookieVersion).where(
        AccountCookieVersion.id == row.id,
        AccountCookieVersion.id == latest_id,
        AccountCookieVersion.encrypted_cookies == row.encrypted_cookies,
    ).values(encrypted_cookies=encrypt_text(json.dumps(
        refreshed, ensure_ascii=False, separators=(',', ':')))),
        execution_options={'synchronize_session': False})
    return result.rowcount == 1


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def enrich_user_info_with_xhs_self_profile(user_info: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data") if isinstance(response, dict) else {}
    if not isinstance(data, dict):
        return user_info
    basic_info = data.get("basic_info")
    if not isinstance(basic_info, dict):
        basic_info = {}

    interaction_counts: dict[str, Any] = {}
    interactions = data.get("interactions")
    if isinstance(interactions, list):
        for item in interactions:
            if not isinstance(item, dict):
                continue
            interaction_type = item.get("type")
            if interaction_type:
                interaction_counts[str(interaction_type)] = _first_present(item.get("i18n_count"), item.get("count"))

    profile = {
        **account_profile_from_user_info(user_info),
        "red_id": _first_present(basic_info.get("red_id"), account_profile_from_user_info(user_info).get("red_id"), ""),
        "description": _first_present(basic_info.get("desc"), account_profile_from_user_info(user_info).get("description"), ""),
        "ip_location": _first_present(basic_info.get("ip_location"), account_profile_from_user_info(user_info).get("ip_location"), ""),
        "gender": _first_present(basic_info.get("gender"), account_profile_from_user_info(user_info).get("gender")),
        "followers": _first_present(interaction_counts.get("fans"), account_profile_from_user_info(user_info).get("followers")),
        "following": _first_present(interaction_counts.get("follows"), account_profile_from_user_info(user_info).get("following")),
        "likes": _first_present(interaction_counts.get("interaction"), account_profile_from_user_info(user_info).get("likes")),
        "raw": response,
    }
    return {
        **user_info,
        "nickname": _first_present(basic_info.get("nickname"), user_info.get("nickname"), ""),
        "avatar_url": _first_present(basic_info.get("images"), basic_info.get("imageb"), user_info.get("avatar_url"), ""),
        "profile": profile,
    }


def serialize_account(account: PlatformAccount, action: str | None = None) -> dict[str, Any]:
    try:
        profile = json.loads(account.profile_json or "{}")
        if not isinstance(profile, dict):
            profile = {}
    except json.JSONDecodeError:
        profile = {}

    payload = {
        "id": account.id,
        "platform": account.platform,
        "sub_type": account.sub_type,
        "external_user_id": account.external_user_id,
        "nickname": account.nickname,
        "avatar_url": account.avatar_url,
        "status": account.status,
        "status_message": account.status_message,
        "profile": profile,
        "created_at": account.created_at.isoformat() if account.created_at else None,
        "updated_at": (account.updated_at or account.created_at).isoformat() if (account.updated_at or account.created_at) else None,
    }
    if action:
        payload["action"] = action
    return payload


def require_relogin_account(db: Session, user_id: int, platform: str, sub_type: str,
                            account_id: int | None) -> PlatformAccount | None:
    if account_id is None:
        return None
    account = db.get(PlatformAccount, account_id)
    if account is None or account.user_id != user_id or account.platform != platform:
        raise HTTPException(404, "待重新登录的账号不存在")
    if account.sub_type != sub_type:
        raise HTTPException(400, "请使用原账号的登录类型重新登录")
    return account


def upsert_platform_account_from_login(
    *,
    db: Session,
    user_id: int,
    platform: str,
    sub_type: str,
    user_info: dict[str, Any],
    cookies_text: str,
    account_id: int | None = None,
) -> tuple[PlatformAccount, str]:
    external_user_id = user_info.get("external_user_id", "") or ""
    account = require_relogin_account(db, user_id, platform, sub_type, account_id)
    if account is not None and (not external_user_id or external_user_id != account.external_user_id):
        raise HTTPException(409, "登录的账号与原账号不一致，请切换到原小红书账号后重新登录")
    if account is None and external_user_id:
        account = db.scalar(
            select(PlatformAccount).where(
                PlatformAccount.user_id == user_id,
                PlatformAccount.platform == platform,
                PlatformAccount.sub_type == sub_type,
                PlatformAccount.external_user_id == external_user_id,
            )
        )

    action = "updated" if account is not None else "created"
    now = shanghai_now()
    if account is None:
        account = PlatformAccount(
            user_id=user_id,
            platform=platform,
            sub_type=sub_type,
            external_user_id=external_user_id,
            created_at=now,
        )
        db.add(account)

    account.nickname = user_info.get("nickname", "") or account.nickname or ""
    account.avatar_url = user_info.get("avatar_url", "") or account.avatar_url or ""
    account.external_user_id = external_user_id or account.external_user_id
    account.status = "active"
    account.status_message = ""
    account.profile_json = json.dumps(account_profile_from_user_info(user_info), ensure_ascii=False, separators=(",", ":"))
    account.updated_at = now
    db.flush()
    db.add(
        AccountCookieVersion(
            platform_account_id=account.id,
            encrypted_cookies=encrypt_text(cookies_text),
        )
    )
    return account, action
