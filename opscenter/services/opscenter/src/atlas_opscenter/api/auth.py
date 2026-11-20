"""登录（账号 + 密码；用户 11-20 定不做双因素）、登出、本人、改密码。"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from ..accounts import Accounts, Principal
from .deps import CSRF_COOKIE, SESSION_COOKIE, OperatorDep, SessionDep, SettingsDep

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    account: str
    password: str


class PasswordBody(BaseModel):
    old_password: str
    new_password: str


def me_view(p: Principal) -> dict[str, Any]:
    return {
        "id": str(p.operator_uuid),
        "account": p.account,
        "name": p.name,
        "role": p.role,
        "must_change_password": p.must_change_password,
    }


@router.post("/login")
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    ip = request.client.host if request.client else None
    token, op = await Accounts(session, settings).login(body.account, body.password, ip)
    cookie = {
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": "lax",
        "path": settings.cookie_path,
    }
    response.set_cookie(SESSION_COOKIE, token, **cookie)  # type: ignore[arg-type]
    # CSRF 双提交：前端读这个 Cookie，写请求带同值的请求头
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(16),
        secure=settings.cookie_secure,
        samesite="lax",
        path=settings.cookie_path,
    )
    return {
        "id": str(op.operator_uuid),
        "account": op.account,
        "name": op.name,
        "role": op.role,
        "must_change_password": op.must_change_password,
    }


@router.post("/logout")
async def logout(
    request: Request, response: Response, session: SessionDep, settings: SettingsDep
) -> dict[str, bool]:
    await Accounts(session, settings).logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path=settings.cookie_path)
    response.delete_cookie(CSRF_COOKIE, path=settings.cookie_path)
    return {"ok": True}


@router.get("/me")
async def me(p: OperatorDep) -> dict[str, Any]:
    return me_view(p)


@router.post("/password")
async def change_password(
    body: PasswordBody, p: OperatorDep, request: Request, session: SessionDep, settings: SettingsDep
) -> dict[str, bool]:
    token = request.cookies.get(SESSION_COOKIE) or ""
    await Accounts(session, settings).change_password(
        p, body.old_password, body.new_password, token
    )
    return {"ok": True}
