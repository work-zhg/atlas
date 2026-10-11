"""每个请求解析一次主体（权限设计 §4）。

顺序：
  1. Authorization: Bearer atlas_sk_…  → 服务账号（两种模式都接受）
  2. auth_mode = uc：会话 Cookie atlas_session → 用户；写请求校验 CSRF 双提交；X-User-Id 一律忽略
  3. auth_mode = dev：X-User-Id 请求头，缺省 default_user_id；操作码全有（本机开发、单测）

★ 失败一律 401，原因只写日志（不告诉调用方是过期、伪造还是停用）。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db.session import get_session
from ..errors import Forbidden, Unauthorized
from .catalog import ALL_OPERATIONS
from .principal import Principal
from .service import SERVICE_KEY_PREFIX, AuthService

__all__ = ["CSRF_COOKIE", "CSRF_HEADER", "SESSION_COOKIE", "current_principal", "uc_client"]

SESSION_COOKIE = "atlas_session"
CSRF_COOKIE = "atlas_csrf"
CSRF_HEADER = "x-atlas-csrf"
_SAFE = {"GET", "HEAD", "OPTIONS"}


def uc_client(request: Request):  # type: ignore[no-untyped-def]
    """进程级用户中心客户端（lifespan 里建；未接入时为 None）。"""
    return getattr(request.app.state, "uc", None)


async def current_principal(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    x_user_id: Annotated[UUID | None, Header(alias="X-User-Id", include_in_schema=False)] = None,
) -> Principal:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        key = auth[7:].strip()
        if not key.startswith(SERVICE_KEY_PREFIX):
            raise Unauthorized("凭据无效")
        svc = AuthService(session, settings, uc_client(request))
        return await svc.resolve_service_key(key, request.headers.get("x-atlas-acting-user"))

    if settings.auth_mode == "uc":
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise Unauthorized("请先登录")
        principal = await AuthService(session, settings, uc_client(request)).resolve_session(token)
        if request.method not in _SAFE:
            cookie = request.cookies.get(CSRF_COOKIE)
            if not cookie or cookie != request.headers.get(CSRF_HEADER):
                raise Forbidden("请求校验失败，请刷新页面后重试")
        return principal

    # dev：沿用原来的请求头 + 默认用户；操作码全有
    return Principal(
        kind="user",
        owner_id=x_user_id or settings.default_user_id,
        name="dev",
        operations=ALL_OPERATIONS,
    )
