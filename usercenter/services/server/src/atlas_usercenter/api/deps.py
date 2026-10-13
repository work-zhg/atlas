"""依赖：数据库会话、管理台登录态（含 CSRF 与强制改密）、操作码鉴权、开放接口的应用身份。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..app.service import AppService
from ..audit import Actor
from ..builtin import uc_app_uuid
from ..db.models import App, Credential, Session, User
from ..db.session import get_session
from ..errors import Forbidden, Unauthorized
from ..perm.effective import authz_snapshot
from ..settings import UCSettings, get_settings

__all__ = [
    "CSRF_COOKIE",
    "SESSION_COOKIE",
    "OpenAppDep",
    "Principal",
    "PrincipalDep",
    "SessionDep",
    "SettingsDep",
    "require",
]

SESSION_COOKIE = "uc_session"
CSRF_COOKIE = "uc_csrf"
CSRF_HEADER = "x-uc-csrf"
_SAFE = {"GET", "HEAD", "OPTIONS"}
#: 须改密时仍可访问的接口
_CHANGE_ALLOWED = {"/api/v1/auth/me", "/api/v1/auth/password", "/api/v1/auth/logout"}

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[UCSettings, Depends(get_settings)]


@dataclass
class Principal:
    user: User
    session_row: Session
    credential: Credential
    permissions: set[str]
    menus: list[dict]  # type: ignore[type-arg]

    @property
    def actor(self) -> Actor:
        return Actor.user(self.user.uuid)

    def has(self, op: str) -> bool:
        return op in self.permissions


async def current_principal(
    request: Request, session: SessionDep, settings: SettingsDep
) -> Principal:
    from ..auth.service import AuthService

    user, sess, cred = await AuthService(session, settings).resolve(
        request.cookies.get(SESSION_COOKIE)
    )
    if request.method not in _SAFE:
        cookie = request.cookies.get(CSRF_COOKIE)
        if not cookie or cookie != request.headers.get(CSRF_HEADER):
            raise Forbidden("CSRF_FAILED", "请求校验失败，请刷新页面后重试")
    if cred.must_change_password and request.url.path not in _CHANGE_ALLOWED:
        raise Forbidden("PASSWORD_CHANGE_REQUIRED", "请先修改密码")
    app = (
        await session.execute(select(App).where(App.uuid == await uc_app_uuid(session)))
    ).scalar_one()
    snap = await authz_snapshot(session, user, app)
    return Principal(user, sess, cred, set(snap["permissions"]), snap["menus"])


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require(*ops: str):  # type: ignore[no-untyped-def]
    """操作码鉴权：前端隐藏只负责「看不到」，接口在这里再鉴权一次。"""

    async def _dep(principal: PrincipalDep) -> Principal:
        missing = [op for op in ops if not principal.has(op)]
        if missing:
            raise Forbidden(
                "PERMISSION_DENIED", f"缺少操作权限：{'、'.join(missing)}", missing=missing
            )
        return principal

    return Depends(_dep)


async def current_open_app(request: Request, session: SessionDep, settings: SettingsDep) -> App:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    if not token:
        raise Unauthorized("INVALID_TOKEN", "缺少接口令牌（Authorization: Bearer）")
    return await AppService(session, Actor.system(), settings).resolve_token(token)


OpenAppDep = Annotated[App, Depends(current_open_app)]
