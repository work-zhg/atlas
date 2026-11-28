"""出站身份：本服务调用户中心、Agent 平台时带什么请求头。

    use_identity(uid)       之后本任务发出的内网调用都以这个人的名义（X-Uid）
    acting_as(uid)          临时换一个人（如以接入人的身份授权、以作者的身份开 run），出块还原
    outbound_headers()      X-Uid、X-Tenant-Id（当前租户）、X-Caller-App: teamflow；不写 X-Auth-Uid
                            （按谁的权限判由 Agent 平台按智能体决定）

★ 租户取 atlas_tenancy 的当前租户：请求入口进入了租户、后台按数据所属的租户逐条进入，
  调下游时自然带上，不用每处传。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from atlas_tenancy import TENANT_HEADER, TenantScope

__all__ = ["CALLER_APP", "IdentityScopeMiddleware", "acting_as", "outbound_headers", "use_identity"]

#: 本服务的应用编码（用户中心 uc_app.code）
CALLER_APP = "teamflow"

_uid: ContextVar[str | None] = ContextVar("tf_outbound_uid", default=None)


def use_identity(uid: UUID | str | None) -> None:
    _uid.set(str(uid) if uid else None)


@contextmanager
def acting_as(uid: UUID | str | None) -> Iterator[None]:
    token = _uid.set(str(uid) if uid else None)
    try:
        yield
    finally:
        _uid.reset(token)


def outbound_headers() -> dict[str, str]:
    h = {"X-Caller-App": CALLER_APP}
    uid = _uid.get()
    if uid:
        h["X-Uid"] = uid
    tenant = TenantScope.current()
    if tenant is not None:
        h[TENANT_HEADER] = str(tenant)
    return h


class IdentityScopeMiddleware:
    """每个请求一个干净的出站身份，结束时还原（纯 ASGI）。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = _uid.set(None)
        try:
            with TenantScope.isolated():
                await self.app(scope, receive, send)
        finally:
            _uid.reset(token)
