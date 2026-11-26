"""认证（内网身份传递 §3、§8；知识库设计 §11）。

内网免令牌：身份在请求头里 —— 网关按登录态写入；atlas-server 运行时调用时写这一轮的身份。
  X-Uid         谁在用（建库人、上传人、检索记录里的发起人）
  X-Auth-Uid    按谁的权限判，缺省 = X-Uid：库级权限按它（只剩这一套规则）
  X-Tenant-Id   租户（必填）：带了 X-Uid 的经用户中心核对「这个人在这个租户里」，然后进入这个
                租户，数据按它自动过滤（atlas_tenancy）。不知道租户状态：租户停用、到期后，
                用户中心给这个人的快照里没有 Atlas 的访问权 → 403
  X-Caller-App  直接调用方：gateway / atlas …

  缺 X-Uid → 401；操作码按 auth-uid 取用户中心授权快照（以 Atlas 应用的身份，经
  usercenter.UserCenterGateway —— 调用户中心的唯一客户端）。
  没有免鉴权的模式（原 dev 模式已删除）：测试里用户中心是替身（tests/conftest.py）。

接口按功能分（管理接口、检索接口，都在 /v1），不按调用方分：浏览器经网关、Atlas、MCP 网关调的是
同一套接口，规则也只有这一套。本服务不判断请求是不是网关转来的 —— 请求头可不可信是网关和平台
内网约定的事。

★ 出站透传：本服务调用户中心时原样带上本次请求的身份请求头，X-Caller-App 写
  atlas（知识库属于 Atlas 应用，数据编码登记在它名下）。
★ 与 atlas-server、atlas-skill 各自实现，不抽公共包（规则只有几十行）。
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Annotated, Any
from uuid import UUID

from atlas_tenancy import TENANT_HEADER, TenantScope, parse_tenant
from fastapi import Depends, HTTPException, Request

from ..errors import NotFound, Unavailable
from ..settings import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "IdentityHeadersMiddleware",
    "Principal",
    "current_principal",
    "outbound_headers",
    "use_identity",
    "verified_tenant",
]

#: 入站（小写）→ 出站请求头
_IDENTITY_HEADERS = {"x-uid": "X-Uid", "x-auth-uid": "X-Auth-Uid", "x-tenant-id": "X-Tenant-Id"}

#: 本服务关心的操作码（在 atlas-server auth/catalog.py 登记到用户中心）。
#: ★ 没有「管理全部知识库」的操作码：那是数据权限（用户中心的「全部知识库」授权，perms.py）
KB_OPERATIONS = frozenset({"kb:create"})


@dataclass(frozen=True)
class Principal:
    #: 谁在用（X-Uid）
    uid: str
    #: 按谁的权限判（X-Auth-Uid）。不给 = uid
    auth_uid: str = ""
    caller_app: str = "unknown"
    operations: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.auth_uid:
            object.__setattr__(self, "auth_uid", self.uid)

    def has(self, op: str) -> bool:
        return op in self.operations


# ───────────────────────────── 出站身份


_outbound: ContextVar[dict[str, str] | None] = ContextVar("kb_outbound_identity", default=None)


def use_identity(uid: str | None, auth_uid: str | None = None, tenant: str | None = None) -> None:
    """设置本任务之后的出站身份：没有入站请求、又要以某人的身份调用户中心时用。"""
    h = {}
    if uid:
        h["X-Uid"] = uid
        h["X-Auth-Uid"] = auth_uid or uid
    if tenant:
        h["X-Tenant-Id"] = tenant
    _outbound.set(h or None)


def outbound_headers() -> dict[str, str]:
    """出站请求头。没透传到租户（入库任务）时写当前租户。"""
    headers = {**(_outbound.get() or {}), "X-Caller-App": get_settings().uc_app_code}
    tenant = TenantScope.current()
    if TENANT_HEADER not in headers and tenant is not None:
        headers[TENANT_HEADER] = str(tenant)
    return headers


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
            # 每个请求从「没有租户」开始：由 verified_tenant 核对后进入
            with TenantScope.isolated():
                await self.app(scope, receive, send)
        finally:
            _outbound.reset(token)


# ───────────────────────────── 授权快照


async def _snapshot(request: Request, auth_uid: str, tenant: UUID) -> dict[str, Any]:
    """在这个租户里向用户中心取这个人的授权快照（usercenter.py，成功结果短缓存）。

    不在这个租户里、停用、不存在 → 401；用户中心不可用 → 503，不放行。
    """
    try:
        snap: dict[str, Any] = await request.app.state.runtime.user_center.snapshot(
            auth_uid, tenant
        )
    except NotFound as exc:
        raise HTTPException(status_code=401, detail="请先登录") from exc
    except Unavailable as exc:
        raise HTTPException(status_code=503, detail="暂时无法校验权限，请稍后重试") from exc
    return snap


async def verified_tenant(request: Request) -> UUID:
    """管理接口、检索接口的入口：取请求头里的租户，核对后进入它（之后的读写按它自动过滤）。

    缺 X-Uid 一律 401；带了的经用户中心核对他在这个租户里。
    """
    tenant = parse_tenant(request.headers.get("x-tenant-id"))
    if tenant is None:
        raise HTTPException(status_code=401, detail="缺少租户（X-Tenant-Id）")
    h = request.headers
    uid = (h.get("x-uid") or "").strip()
    if not uid:
        # 身份只有请求头一个来源：没有兜底（请求体里的发起人、只信租户请求头都不算）
        raise HTTPException(status_code=401, detail="请先登录")
    auth_uid = (h.get("x-auth-uid") or "").strip() or uid
    snap = await _snapshot(request, auth_uid, tenant)
    home = parse_tenant((snap.get("user") or {}).get("tenant_id"))
    if home is not None and home != tenant:
        raise HTTPException(status_code=401, detail="请求头里的租户和用户所属的不一致")
    TenantScope.bind(tenant)
    return tenant


async def current_principal(
    request: Request, tenant: Annotated[UUID, Depends(verified_tenant)]
) -> Principal:
    h = request.headers
    uid = (h.get("x-uid") or "").strip()
    auth_uid = (h.get("x-auth-uid") or "").strip() or uid
    caller = (h.get("x-caller-app") or "").strip() or "unknown"
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    snap = await _snapshot(request, auth_uid, tenant)
    user = snap.get("user") or {}
    if user.get("status") == "disabled":
        raise HTTPException(status_code=401, detail="账号已停用")
    if not snap.get("can_access"):
        raise HTTPException(status_code=403, detail="没有使用 Atlas 的权限")
    ops = frozenset(snap.get("permissions") or [])
    return Principal(uid=uid, auth_uid=auth_uid, caller_app=caller, operations=ops)
