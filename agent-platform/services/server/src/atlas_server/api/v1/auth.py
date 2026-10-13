"""登录（用户中心代为校验密码）、登出、当前主体（权限设计 §4.1、§12）。"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from ...auth.authz import PrincipalDep
from ...auth.dependency import CSRF_COOKIE, SESSION_COOKIE, uc_client
from ...auth.principal import Principal
from ...auth.service import AuthService
from ...db.models import AppUser
from ...deps import SessionDep, SettingsDep
from ...errors import NotFound

router = APIRouter(tags=["auth"])


class LoginBody(BaseModel):
    account: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


async def _me(session: SessionDep, settings: SettingsDep, p: Principal) -> dict[str, Any]:
    out: dict[str, Any] = {
        "auth_mode": settings.auth_mode,
        "kind": p.kind,
        "id": str(p.owner_id),
        "name": p.name,
        "operations": sorted(p.operations),
        "menus": list(p.menus),
    }
    if p.kind == "user":
        user = await session.get(AppUser, p.owner_id)
        if user is not None:
            out.update(
                name=user.name,
                account=user.account,
                email=user.email,
                dept=user.dept_name,
            )
    return out


@router.post("/auth/login")
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    if settings.auth_mode != "uc":
        raise NotFound("当前部署不需要登录（dev 模式）")
    token, principal = await AuthService(session, settings, uc_client(request)).login(
        body.account, body.password, request.headers.get("user-agent")
    )
    max_age = settings.session_hours * 3600
    for name, value, http_only in (
        (SESSION_COOKIE, token, True),
        (CSRF_COOKIE, secrets.token_urlsafe(24), False),  # 前端读出来放进 X-Atlas-CSRF
    ):
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            httponly=http_only,
            secure=settings.auth_cookie_secure,
            samesite="lax",
            path="/",
        )
    await session.commit()
    return await _me(session, settings, principal)


@router.post("/auth/logout")
async def logout(
    request: Request, response: Response, session: SessionDep, settings: SettingsDep
) -> dict[str, bool]:
    await AuthService(session, settings, uc_client(request)).logout(
        request.cookies.get(SESSION_COOKIE)
    )
    await session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(principal: PrincipalDep, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    out = await _me(session, settings, principal)
    await session.commit()  # 会话续期 / 最近访问时间
    return out
