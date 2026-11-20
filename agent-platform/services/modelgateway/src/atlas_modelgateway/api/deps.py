"""接口依赖：会话、用户中心、账号、模型、Key。

★ 会话用 scope="function"：提交在响应发出之前（默认的 request 作用域在响应之后才提交，
  写完立刻读的调用方会读不到）。提交之后配置版本号加一（notify），网关下一次请求就用新配置。
★ 每个依赖都在 verified_tenant 之后：查询按当前租户自动过滤。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..accounts import AccountService
from ..auth import TenantDep
from ..calls import CallLog
from ..crypto import SecretCipher
from ..db.session import get_session
from ..gateway.format import AddressGuard, UpstreamClient
from ..gateway.routing import RouteHealth
from ..keys import KeyService
from ..perms import Permissions, SubjectsCache
from ..provider import ProviderCatalog
from ..registry import ModelRegistry
from ..settings import GatewaySettings, get_settings

__all__ = [
    "AccountsDep",
    "CallsDep",
    "CatalogDep",
    "HealthDep",
    "KeysDep",
    "PermsDep",
    "ProbeClientDep",
    "RegistryDep",
    "SessionDep",
    "SettingsDep",
]


async def _session(
    _: TenantDep, session: Annotated[AsyncSession, Depends(get_session, scope="function")]
) -> AsyncSession:
    return session


SessionDep = Annotated[AsyncSession, Depends(_session)]
SettingsDep = Annotated[GatewaySettings, Depends(get_settings)]


def _state(name: str):
    def get(request: Request):
        return getattr(request.app.state, name)

    return get


PermsDep = Annotated[Permissions, Depends(_state("perms"))]
CatalogDep = Annotated[ProviderCatalog, Depends(_state("catalog"))]
HealthDep = Annotated[RouteHealth, Depends(_state("health"))]
ProbeClientDep = Annotated[UpstreamClient, Depends(_state("probe_client"))]


def get_registry(session: SessionDep, perms: PermsDep) -> ModelRegistry:
    return ModelRegistry(session, perms)


RegistryDep = Annotated[ModelRegistry, Depends(get_registry)]


def get_keys(
    session: SessionDep,
    registry: RegistryDep,
    subjects: Annotated[SubjectsCache, Depends(_state("subjects"))],
    cipher: Annotated[SecretCipher, Depends(_state("cipher"))],
) -> KeyService:
    return KeyService(session, registry, subjects, cipher)


KeysDep = Annotated[KeyService, Depends(get_keys)]


def get_accounts(
    session: SessionDep,
    catalog: CatalogDep,
    health: HealthDep,
    cipher: Annotated[SecretCipher, Depends(_state("cipher"))],
    guard: Annotated[AddressGuard, Depends(_state("guard"))],
) -> AccountService:
    return AccountService(session, cipher=cipher, catalog=catalog, guard=guard, health=health)


AccountsDep = Annotated[AccountService, Depends(get_accounts)]


def get_calls(session: SessionDep) -> CallLog:
    return CallLog(session)


CallsDep = Annotated[CallLog, Depends(get_calls)]
