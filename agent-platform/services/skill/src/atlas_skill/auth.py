"""认证（设计 §14；权限设计 §9；内网身份传递 §3）。

内网免令牌：身份在请求头里（网关按登录态写入 X-Uid；atlas-server 运行时调用时写这一轮的身份）。

  /api/v1/*    用户（经网关）。缺 X-Uid 一律 401；操作码按 X-Auth-Uid（缺省 = X-Uid）取
               用户中心授权快照（以本应用的身份：X-Caller-App: skill）。不在本应用可访问
               范围内的人一律 403。登录、CSRF 在网关 + 用户中心，这里不再处理。
  /internal/*  运行时进程。网关一律 404（只在内网可达）；不再要服务间令牌。

租户（多租户设计 §5）：两类接口都要 X-Tenant-Id。带了用户的，以本次请求的租户向用户中心取快照
（用户中心只在这个租户里找这个人，找不到就是 401），然后进入这个租户，数据按它自动过滤。租户已停用
（运营接口）的一律 403。

★ 与运行时各自实现，不抽公共包：规则只有几十行，抽出来就是第三个所有服务都
  依赖的包，收益抵不上耦合。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Annotated, Any
from uuid import UUID

import httpx
from atlas_tenancy import TENANT_HEADER, TenantScope, parse_tenant
from fastapi import Depends, HTTPException, Request

from .settings import SkillSettings, get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIT_VIEW",
    "SKILL_REVIEW",
    "SKILL_UPLOAD",
    "Principal",
    "current_principal",
    "require_any",
    "verified_tenant",
]

#: 操作码（本应用名下；由运营人员在运营中心录入，见 README「用户中心登记」）
SKILL_UPLOAD = "skill:upload"
#: 审查、停用 / 启用、下架、重新检查
SKILL_REVIEW = "skill:review"
AUDIT_VIEW = "audit:view"

_FORWARD = {"x-uid": "X-Uid", "x-auth-uid": "X-Auth-Uid", "x-tenant-id": "X-Tenant-Id"}


@dataclass(frozen=True)
class Principal:
    #: 谁在用（X-Uid）：上传人、审查人
    user: str
    operations: frozenset[str] = field(default_factory=frozenset)

    def has_any(self, *ops: str) -> bool:
        return any(op in self.operations for op in ops)


@dataclass(frozen=True)
class _Snapshot:
    operations: frozenset[str]
    #: 用户中心给的用户所属租户
    tenant: UUID | None


class _Cache:
    """(auth-uid, 租户) → (过期时刻, 快照)。只缓存成功结果；失败每次都问。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, UUID], tuple[float, _Snapshot]] = {}

    def get(self, key: tuple[str, UUID]) -> _Snapshot | None:
        row = self._rows.get(key)
        if row is None or row[0] < time.monotonic():
            self._rows.pop(key, None)
            return None
        return row[1]

    def put(self, key: tuple[str, UUID], snap: _Snapshot, ttl: float) -> None:
        if len(self._rows) > 10_000:  # 防止无界增长
            self._rows.clear()
        self._rows[key] = (time.monotonic() + ttl, snap)


_cache = _Cache()
#: 测试替换用（生产为 None，走真实网络）
_transport: httpx.AsyncBaseTransport | None = None


async def _snapshot(
    settings: SkillSettings, request: Request, auth_uid: str, tenant: UUID
) -> _Snapshot:
    hit = _cache.get((auth_uid, tenant))
    if hit is not None:
        return hit
    # 透传本次请求的身份（用户中心据此建立租户上下文）
    headers: dict[str, Any] = {
        out: request.headers[name] for name, out in _FORWARD.items() if request.headers.get(name)
    }
    headers["X-Caller-App"] = settings.uc_app_code
    headers[TENANT_HEADER] = str(tenant)
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False, transport=_transport) as client:
            r = await client.get(
                f"{settings.uc_base_url.rstrip('/')}/open/v1/users/{auth_uid}/authz",
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.warning("取授权快照失败（用户中心不可达）：%s", exc)
        raise HTTPException(status_code=503, detail="暂时无法校验权限，请稍后重试") from exc
    if r.status_code in (401, 404):
        raise HTTPException(status_code=401, detail="请先登录")
    if r.status_code != 200:
        logger.warning("授权快照返回 %s", r.status_code)
        raise HTTPException(status_code=503, detail="暂时无法校验权限，请稍后重试")
    body = r.json()
    user = body.get("user") or {}
    if user.get("status") == "disabled":
        raise HTTPException(status_code=401, detail="账号已停用")
    if not body.get("can_access"):
        raise HTTPException(status_code=403, detail="没有使用「技能」应用的权限")
    snap = _Snapshot(frozenset(body.get("permissions") or []), parse_tenant(user.get("tenant_id")))
    _cache.put((auth_uid, tenant), snap, settings.authz_ttl_s)
    return snap


async def verified_tenant(
    request: Request, settings: Annotated[SkillSettings, Depends(get_settings)]
) -> UUID:
    """管理接口、运行时接口的入口：取请求头里的租户，核对后进入它。

    带了用户的经用户中心核对他在这个租户里；没带的（运行时替自己的租户调）按请求头。
    """
    tenant = parse_tenant(request.headers.get("x-tenant-id"))
    if tenant is None:
        raise HTTPException(status_code=401, detail="缺少租户（X-Tenant-Id）")
    uid = (request.headers.get("x-uid") or "").strip()
    if uid:
        auth_uid = (request.headers.get("x-auth-uid") or "").strip() or uid
        snap = await _snapshot(settings, request, auth_uid, tenant)
        if snap.tenant is not None and snap.tenant != tenant:
            raise HTTPException(status_code=401, detail="请求头里的租户和用户所属的不一致")
    gate = getattr(request.app.state, "tenant_gate", None)
    if gate is not None and not await gate.is_active(tenant):
        raise HTTPException(status_code=403, detail="所在的组织已停用")
    TenantScope.bind(tenant)
    return tenant


async def current_principal(
    request: Request,
    settings: Annotated[SkillSettings, Depends(get_settings)],
    tenant: Annotated[UUID, Depends(verified_tenant)],
) -> Principal:
    uid = (request.headers.get("x-uid") or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    auth_uid = (request.headers.get("x-auth-uid") or "").strip() or uid
    snap = await _snapshot(settings, request, auth_uid, tenant)
    return Principal(user=uid, operations=snap.operations)


def require_any(*ops: str):  # type: ignore[no-untyped-def]
    """有其中任一操作码即可（前端隐藏只负责「看不到」）。"""

    def dep(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if not principal.has_any(*ops):
            raise HTTPException(status_code=403, detail="没有权限执行该操作")
        return principal

    return dep
