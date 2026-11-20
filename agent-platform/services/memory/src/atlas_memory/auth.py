"""身份（内网身份传递 §3）：请求头里的调用用户，以及作用域管理接口的操作码。

    X-Uid         谁在用
    X-Auth-Uid    按谁的权限判，缺省 = X-Uid。数据权限（§3.7）按它校验
    X-Tenant-Id   租户（必填）：带了 X-Uid 的请求经用户中心核对「这个人在这个租户里」，然后进入
                  这个租户，数据按它自动过滤（atlas_tenancy）
    X-Caller-App  直接调用方（atlas …）。★ 只用于追溯：本服务不做应用级访问控制，
                  允许多个客户端读写同一份数据（§3.2）

★ 「谁在访问」和「访问哪个实例」分开（§3.7）：实例永远由接口参数给出（作用域编码 + 关联主键），
  请求头只在作用域打开数据权限开关时用来回答「这个人能不能访问」。
★ 出站透传：本服务调用户中心时原样带上本次请求的身份请求头，X-Caller-App 换成本服务的应用编码。
★ 与其它服务各自实现，不抽公共包（规则只有几十行）。
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

import httpx
from atlas_tenancy import TENANT_HEADER, TenantScope, parse_tenant
from fastapi import Depends, Request

from .errors import Forbidden, Unauthorized, Unavailable
from .settings import MemorySettings, get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "Caller",
    "IdentityHeadersMiddleware",
    "caller_of",
    "outbound_headers",
    "verified_tenant",
]

#: 入站（小写）→ 出站请求头
_IDENTITY_HEADERS = {"x-uid": "X-Uid", "x-auth-uid": "X-Auth-Uid", "x-tenant-id": "X-Tenant-Id"}


@dataclass(frozen=True)
class Caller:
    #: 谁在用（X-Uid）
    uid: str
    #: 按谁的权限判（X-Auth-Uid，缺省 = uid）
    auth_uid: str
    caller_app: str = "unknown"


def caller_of(request: Request) -> Caller | None:
    """请求里的调用用户；没有 X-Uid 时为 None（数据权限开关关闭的作用域不需要它）。"""
    h = request.headers
    uid = (h.get("x-uid") or "").strip()
    if not uid:
        return None
    auth_uid = (h.get("x-auth-uid") or "").strip() or uid
    return Caller(uid=uid, auth_uid=auth_uid, caller_app=(h.get("x-caller-app") or "unknown"))


# ───────────────────────────── 出站身份


_outbound: ContextVar[dict[str, str] | None] = ContextVar("memory_outbound_identity", default=None)


def outbound_headers() -> dict[str, str]:
    return {**(_outbound.get() or {}), "X-Caller-App": get_settings().uc_app_code}


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
            # 每个请求从「没有租户」开始，由 verified_tenant 进入
            with TenantScope.isolated():
                await self.app(scope, receive, send)
        finally:
            _outbound.reset(token)


# ───────────────────────────── 租户


class _Cache:
    """(auth-uid, 租户) → (过期时刻, 用户中心给的用户资料)。只缓存成功结果。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, UUID], tuple[float, dict[str, Any]]] = {}

    def get(self, key: tuple[str, UUID]) -> dict[str, Any] | None:
        row = self._rows.get(key)
        if row is None or row[0] < time.monotonic():
            self._rows.pop(key, None)
            return None
        return row[1]

    def put(self, key: tuple[str, UUID], user: dict[str, Any], ttl: float) -> None:
        if len(self._rows) > 10_000:
            self._rows.clear()
        self._rows[key] = (time.monotonic() + ttl, user)


_cache = _Cache()
#: 测试替换用（生产为 None，走真实网络）
_transport: httpx.AsyncBaseTransport | None = None


async def _user_in(settings: MemorySettings, auth_uid: str, tenant: UUID) -> dict[str, Any]:
    """在这个租户里向用户中心取这个人（请求头带着 X-Tenant-Id）；不在这个租户里就是 404。"""
    hit = _cache.get((auth_uid, tenant))
    if hit is not None:
        return hit
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False, transport=_transport) as client:
            r = await client.get(
                f"{settings.uc_base_url.rstrip('/')}/open/v1/users/{auth_uid}/authz",
                headers={**outbound_headers(), TENANT_HEADER: str(tenant)},
            )
    except httpx.HTTPError as exc:
        logger.warning("取授权快照失败（用户中心不可达）：%s", exc)
        raise Unavailable("暂时无法校验身份，请稍后重试") from exc
    if r.status_code in (401, 404):
        raise Unauthorized("请先登录")
    if r.status_code != 200:
        logger.warning("授权快照返回 %s：%s", r.status_code, r.text[:200])
        raise Unavailable("暂时无法校验身份，请稍后重试")
    user = r.json().get("user") or {}
    if user.get("status") == "disabled":
        raise Unauthorized("账号已停用")
    _cache.put((auth_uid, tenant), user, settings.authz_ttl_s)
    return user


async def verified_tenant(
    request: Request, settings: Annotated[MemorySettings, Depends(get_settings)]
) -> UUID:
    """数据接口的入口：取请求头里的租户，核对后进入它（之后的读写按它自动过滤）。

    带了调用用户的，经用户中心核对他在这个租户里；没带的（数据权限开关关闭的作用域）是内网
    服务替自己的租户调用，按请求头。租户已停用（运营接口）的一律 403。
    """
    tenant = parse_tenant(request.headers.get("x-tenant-id"))
    if tenant is None:
        raise Unauthorized("缺少租户（X-Tenant-Id）")
    caller = caller_of(request)
    if caller is not None:
        user = await _user_in(settings, caller.auth_uid, tenant)
        claimed = parse_tenant(user.get("tenant_id"))
        if claimed is not None and claimed != tenant:
            raise Unauthorized("请求头里的租户和用户所属的不一致")
    gate = getattr(request.app.state, "tenant_gate", None)
    if gate is not None and not await gate.is_active(tenant):
        raise Forbidden("所在的组织已停用")
    TenantScope.bind(tenant)
    return tenant
