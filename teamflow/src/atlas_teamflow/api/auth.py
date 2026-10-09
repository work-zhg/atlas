"""登录（由用户中心代为校验密码）、登出、当前用户。"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from .. import identity
from .deps import CSRF_COOKIE, SESSION_COOKIE, PrincipalDep, SessionDep, SettingsDep, UCDep

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginBody(BaseModel):
    account: str
    password: str


def _me(p: identity.Principal) -> dict[str, Any]:
    return {
        "user": {
            "id": str(p.user.uuid),
            "account": p.user.account,
            "name": p.user.name,
            "email": p.user.email,
        },
        "roles": p.snapshot.get("roles", []),
        "permissions": sorted(p.permissions),
        "menus": p.snapshot.get("menus", []),
    }


@router.post("/login")
async def login(
    body: LoginBody, response: Response, session: SessionDep, uc: UCDep, settings: SettingsDep
) -> dict[str, Any]:
    _, token = await identity.login(session, uc, settings, body.account, body.password)
    max_age = settings.session_hours * 3600
    for name, value, http_only in (
        (SESSION_COOKIE, token, True),
        (CSRF_COOKIE, secrets.token_urlsafe(24), False),
    ):
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            httponly=http_only,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
    await session.flush()
    p = await identity.resolve(session, uc, settings, token)
    return _me(p)


@router.post("/logout")
async def logout(request: Request, response: Response, session: SessionDep) -> dict[str, bool]:
    await identity.logout(session, request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(principal: PrincipalDep) -> dict[str, Any]:
    return _me(principal)
