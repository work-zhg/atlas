"""出站身份（内网身份传递 §3、§10）：本服务调用户中心、Atlas 时带什么请求头。

    use_identity(uid, tenant)   之后本任务发出的内网调用都以这个人的名义
    outbound_headers()          X-Uid / X-Tenant-Id 透传，X-Caller-App 写 teamflow；不写 X-Auth-Uid
                                （按谁判由 Atlas 按 Agent 决定）

★ 请求里由主体解析设置（谁在操作）；Agent 监督器投递指令时设为指令的作者、没有作者（系统
  投递）时为流程发起人（§10）。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any
from uuid import UUID

__all__ = ["CALLER_APP", "IdentityScopeMiddleware", "outbound_headers", "use_identity"]

#: 本服务的应用编码（用户中心 uc_app.code）
CALLER_APP = "teamflow"

_current: ContextVar[dict[str, str] | None] = ContextVar("tf_outbound_identity", default=None)


def use_identity(uid: UUID | str | None, tenant: str | None = None) -> None:
    h: dict[str, str] = {}
    if uid:
        h["X-Uid"] = str(uid)
    if tenant:
        h["X-Tenant-Id"] = tenant
    _current.set(h or None)


def outbound_headers() -> dict[str, str]:
    return {**(_current.get() or {}), "X-Caller-App": CALLER_APP}


class IdentityScopeMiddleware:
    """每个请求一个干净的出站身份，结束时还原（纯 ASGI）。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = _current.set(None)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)
