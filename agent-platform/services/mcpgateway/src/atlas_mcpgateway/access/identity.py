"""身份请求头的透传（内网身份传递 §3）：调用户中心时带上本次请求的身份。

    X-Uid         谁在用（管理端经网关登录后由网关写入）
    X-Auth-Uid    按谁的权限判，缺省 = X-Uid
    X-Tenant-Id   租户：管理接口、内部接口带了的进入它；数据面不看它，按 Key 认出租户
    X-Caller-App  直接调用方（atlas …），只用于追溯；出站时换成本服务的应用编码

★ 数据面调用户中心时没有入站身份可透传：用 Key 在用户中心的主体（X-Auth-Uid = Key 的 uid），
  由工具调用设置（use_outbound）。
★ 与其它服务各自实现，不抽公共包（规则只有几十行）。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from atlas_tenancy import TENANT_HEADER, TenantScope

from ..settings import get_settings

__all__ = ["IdentityHeadersMiddleware", "outbound_headers", "use_outbound"]

#: 入站（小写）→ 出站请求头
_IDENTITY_HEADERS = {"x-uid": "X-Uid", "x-auth-uid": "X-Auth-Uid", "x-tenant-id": "X-Tenant-Id"}

_outbound: ContextVar[dict[str, str] | None] = ContextVar("mcpgw_outbound_identity", default=None)


def outbound_headers() -> dict[str, str]:
    """调用户中心带的请求头。没透传到租户（数据面、后台任务）时写当前租户。"""
    headers = {**(_outbound.get() or {}), "X-Caller-App": get_settings().uc_app_code}
    tenant = TenantScope.current()
    if TENANT_HEADER not in headers and tenant is not None:
        headers[TENANT_HEADER] = str(tenant)
    return headers


def use_outbound(headers: dict[str, str] | None) -> None:
    """设置本任务之后调用户中心时带的身份（数据面按 Key 认出的主体设）。"""
    _outbound.set(headers)


class IdentityHeadersMiddleware:
    """每个请求：把入站的身份请求头设为出站身份（透传而不是重算），结束时还原。纯 ASGI。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        got = {
            _IDENTITY_HEADERS[k.decode("latin-1")]: v.decode("latin-1")
            for k, v in scope.get("headers") or []
            if k.decode("latin-1") in _IDENTITY_HEADERS and v
        }
        token = _outbound.set(got or None)
        try:
            # 每个请求从「没有租户」开始：管理接口由 verified_tenant 进入，数据面按 Key 进入
            with TenantScope.isolated():
                await self.app(scope, receive, send)
        finally:
            _outbound.reset(token)
