"""出站身份（内网身份传递 §3）：入站在 auth/dependency.py 解析；这里管「出站带什么请求头」。

    CallIdentity        这一次调用代表谁：uid（谁在用）、auth_uid（按谁判）、租户
    use_identity()      之后本任务发出的内网调用都带这组请求头
    outbound_headers()  出站请求头：X-Uid / X-Auth-Uid / X-Tenant-Id 原样透传，
                        X-Caller-App 换成 atlas

★ 请求里由主体解析设置；run 执行时由执行器按 run 记下的身份设置（执行任务是全新的上下文，
  不继承任何请求）。只有执行器那一处会让 auth_uid 与 uid 不同（P2：Agent 的数字员工）。
★ 用 contextvar 而不是逐层传参：它和 traceparent 一样是「随调用链透传」的东西，
  UC / 知识库 / 配置服务的客户端都从这里取，业务代码不碰。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import UUID

__all__ = [
    "CALLER_APP",
    "CallIdentity",
    "IdentityScopeMiddleware",
    "current_identity",
    "outbound_headers",
    "use_identity",
]

#: 本服务在内网里的应用编码（用户中心 uc_app.code）。知识库、配置服务属于同一个应用
CALLER_APP = "atlas"


@dataclass(frozen=True)
class CallIdentity:
    uid: UUID
    auth_uid: UUID
    tenant_id: str | None = None

    def headers(self) -> dict[str, str]:
        h = {"X-Uid": str(self.uid), "X-Auth-Uid": str(self.auth_uid)}
        if self.tenant_id:
            h["X-Tenant-Id"] = self.tenant_id
        return h


_current: ContextVar[CallIdentity | None] = ContextVar("atlas_call_identity", default=None)


def use_identity(ident: CallIdentity | None) -> None:
    """设置本任务（请求 / run 执行）的出站身份。作用域由 IdentityScopeMiddleware 或执行任务界定。"""
    _current.set(ident)


def current_identity() -> CallIdentity | None:
    return _current.get()


def outbound_headers() -> dict[str, str]:
    ident = _current.get()
    return {**(ident.headers() if ident else {}), "X-Caller-App": CALLER_APP}


class IdentityScopeMiddleware:
    """每个请求一个干净的出站身份，结束时还原（纯 ASGI：不像 BaseHTTPMiddleware 那样另起任务）。"""

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
