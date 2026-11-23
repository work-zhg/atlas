"""接口依赖：会话、对象存储、各领域的应用服务，以及调用身份（访问控制）。

  /api/v1/*    用户（经网关）。缺 X-Uid 一律 401；操作码按 X-Auth-Uid 取用户中心授权快照，不在本应用
               可访问范围内的人一律 403。登录、CSRF 在网关 + 用户中心，这里不再处理。
  /internal/*  运行时进程。网关一律 404（只在内网可达）；内网免令牌。

租户（多租户设计 §5）：两类接口都要 X-Tenant-Id。带了用户的，以本次请求的租户取快照（用户中心只在
这个租户里找这个人，找不到就是 401），然后进入这个租户，数据按它自动过滤。租户已停用（运营接口）的
一律 403。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from atlas_tenancy import TenantScope, parse_tenant
from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..access.control import AccessControl, Principal
from ..db.session import get_session
from ..errors import Forbidden, Unauthorized
from ..market.service import MarketService
from ..settings import SkillSettings, get_settings
from ..store.objects import SkillStorage, StorageNotConfigured, make_storage
from ..versions.service import SkillService

__all__ = [
    "MarketServiceDep",
    "PrincipalDep",
    "SessionDep",
    "SkillServiceDep",
    "StorageDep",
    "current_principal",
    "require_any",
    "verified_tenant",
]

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[SkillSettings, Depends(get_settings)]


def get_storage(request: Request, settings: SettingsDep) -> SkillStorage:
    """进程内单例，测试可预先放进 app.state.storage（moto）。"""
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        try:
            storage = make_storage(settings)
        except StorageNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        request.app.state.storage = storage
    return storage  # type: ignore[no-any-return]


StorageDep = Annotated[SkillStorage, Depends(get_storage)]


def get_skill_service(
    session: SessionDep, storage: StorageDep, settings: SettingsDep
) -> SkillService:
    return SkillService(session=session, storage=storage, settings=settings)


def get_market_service(
    session: SessionDep, storage: StorageDep, settings: SettingsDep
) -> MarketService:
    return MarketService(session=session, storage=storage, settings=settings)


SkillServiceDep = Annotated[SkillService, Depends(get_skill_service)]
MarketServiceDep = Annotated[MarketService, Depends(get_market_service)]


# ───────────────────────────── 租户与调用身份


def get_access(request: Request) -> AccessControl:
    return request.app.state.access  # type: ignore[no-any-return]


async def verified_tenant(
    request: Request, access: Annotated[AccessControl, Depends(get_access)]
) -> UUID:
    """管理接口、运行时接口的入口：取请求头里的租户，核对后进入它。

    带了用户的经用户中心核对他在这个租户里；没带的（运行时替自己的租户调）按请求头。
    """
    tenant = parse_tenant(request.headers.get("x-tenant-id"))
    if tenant is None:
        raise Unauthorized("缺少租户（X-Tenant-Id）")
    request.state.principal = await access.principal(request.headers, tenant)
    gate = getattr(request.app.state, "tenant_gate", None)
    if gate is not None and not await gate.is_active(tenant):
        raise Forbidden("所在的组织已停用")
    TenantScope.bind(tenant)
    return tenant


async def current_principal(
    request: Request, _: Annotated[UUID, Depends(verified_tenant)]
) -> Principal:
    principal: Principal | None = request.state.principal
    if principal is None:
        raise Unauthorized("请先登录")
    return principal


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require_any(*ops: str):  # type: ignore[no-untyped-def]
    """有其中任一操作码即可（前端隐藏只负责「看不到」）。"""

    def dep(principal: PrincipalDep) -> Principal:
        if not principal.has_any(*ops):
            raise Forbidden("没有权限执行该操作")
        return principal

    return dep
