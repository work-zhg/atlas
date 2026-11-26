"""接口依赖：会话、各领域的应用服务（经组合根 wiring.py 拼），以及管理端的租户和操作人。

X-Tenant-Id   租户（必填）：带了 X-Uid 的经授权核对「这个人在这个租户里」，然后进入这个租户，
              数据按它自动过滤（atlas_tenancy）。数据面不看它：按 Key 认出租户
X-Uid         谁在用；X-Auth-Uid 按谁的权限判，缺省 = X-Uid
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from atlas_tenancy import TenantScope, parse_tenant
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .. import wiring
from ..access.authorization import Authorization, Operator
from ..access.key_access import AccessCache
from ..access.usercenter import UserCenter
from ..catalog.service import ServiceCatalog
from ..db.session import get_session
from ..errors import Forbidden, Unauthorized
from ..invocation.service import ToolInvocation
from ..keys.policy import ClientTypePolicy
from ..keys.service import ApiKeys
from ..settings import GatewaySettings, get_settings
from ..upstream.gateway import UpstreamGateway

__all__ = [
    "AuthzDep",
    "CatalogDep",
    "InvocationDep",
    "KeysDep",
    "ManagerDep",
    "OperatorDep",
    "PolicyDep",
    "SessionDep",
    "SettingsDep",
    "verified_tenant",
]

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[GatewaySettings, Depends(get_settings)]


def get_uc(request: Request) -> UserCenter:
    """进程内单例（app 生命周期里建好；测试注入内存实现）。"""
    return request.app.state.uc  # type: ignore[no-any-return]


def get_cache(request: Request) -> AccessCache:
    return request.app.state.access  # type: ignore[no-any-return]


def get_upstream(request: Request) -> UpstreamGateway:
    return request.app.state.upstream  # type: ignore[no-any-return]


def get_invocation(request: Request) -> ToolInvocation:
    return request.app.state.invocation  # type: ignore[no-any-return]


UcDep = Annotated[UserCenter, Depends(get_uc)]
CacheDep = Annotated[AccessCache, Depends(get_cache)]
UpstreamDep = Annotated[UpstreamGateway, Depends(get_upstream)]
InvocationDep = Annotated[ToolInvocation, Depends(get_invocation)]


def get_catalog(
    session: SessionDep, settings: SettingsDep, uc: UcDep, upstream: UpstreamDep
) -> ServiceCatalog:
    return wiring.catalog(session, settings, uc, upstream)


def get_authz(session: SessionDep, uc: UcDep, cache: CacheDep) -> Authorization:
    return wiring.authorization(session, uc, cache)


CatalogDep = Annotated[ServiceCatalog, Depends(get_catalog)]
AuthzDep = Annotated[Authorization, Depends(get_authz)]


def get_keys(session: SessionDep, authz: AuthzDep) -> ApiKeys:
    return ApiKeys(session, authz)


def get_policy(settings: SettingsDep) -> ClientTypePolicy:
    return ClientTypePolicy(settings.client_types)


KeysDep = Annotated[ApiKeys, Depends(get_keys)]
PolicyDep = Annotated[ClientTypePolicy, Depends(get_policy)]


# ───────────────────────────── 租户与操作人


def _identity(request: Request) -> tuple[str, str] | None:
    h = request.headers
    uid = (h.get("x-uid") or "").strip()
    if not uid:
        return None
    return uid, (h.get("x-auth-uid") or "").strip() or uid


async def verified_tenant(request: Request, authz: AuthzDep) -> UUID:
    """管理接口、内部接口的入口：取请求头里的租户，核对后进入它（之后的读写按它自动过滤）。

    带了用户的经授权核对他在这个租户里；没带的（应用替自己的租户调内部接口）按请求头。
    不知道租户状态：租户停用、到期后，用户中心给的授权快照里没有操作码，私有服务的授权也为空。
    """
    tenant = parse_tenant(request.headers.get("x-tenant-id"))
    if tenant is None:
        raise Unauthorized("缺少租户（X-Tenant-Id）")
    who = _identity(request)
    if who is not None:
        await authz.operator(who[0], who[1], tenant)
    TenantScope.bind(tenant)
    return tenant


async def current_operator(
    request: Request, authz: AuthzDep, tenant: Annotated[UUID, Depends(verified_tenant)]
) -> Operator:
    who = _identity(request)
    if who is None:
        raise Unauthorized("请先登录")
    return await authz.operator(who[0], who[1], tenant)


async def require_manager(operator: Annotated[Operator, Depends(current_operator)]) -> Operator:
    if not operator.can_manage:
        raise Forbidden("没有 MCP 管理权限（mcp:manage）")
    return operator


OperatorDep = Annotated[Operator, Depends(current_operator)]
ManagerDep = Annotated[Operator, Depends(require_manager)]
