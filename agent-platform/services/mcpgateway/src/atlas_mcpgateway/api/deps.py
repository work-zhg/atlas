"""接口依赖：会话、注册表、Key、授权、用户中心、上游。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..dataplane import DataPlane
from ..db.session import get_session
from ..grants import GrantService
from ..keys import KeyService
from ..perms import AccessCache, Permissions
from ..registry import Registry
from ..settings import GatewaySettings, get_settings
from ..upstreams.hub import Upstreams

__all__ = [
    "AccessDep",
    "DataPlaneDep",
    "GrantsDep",
    "KeysDep",
    "PermsDep",
    "RegistryDep",
    "SessionDep",
    "SettingsDep",
]

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[GatewaySettings, Depends(get_settings)]


def get_perms(request: Request) -> Permissions:
    """进程内单例（app 生命周期里建好；测试注入内存实现）。"""
    return request.app.state.perms  # type: ignore[no-any-return]


def get_access(request: Request) -> AccessCache:
    return request.app.state.access  # type: ignore[no-any-return]


def get_upstreams(request: Request) -> Upstreams:
    return request.app.state.upstreams  # type: ignore[no-any-return]


def get_data_plane(request: Request) -> DataPlane:
    return request.app.state.data_plane  # type: ignore[no-any-return]


PermsDep = Annotated[Permissions, Depends(get_perms)]
AccessDep = Annotated[AccessCache, Depends(get_access)]
UpstreamsDep = Annotated[Upstreams, Depends(get_upstreams)]
DataPlaneDep = Annotated[DataPlane, Depends(get_data_plane)]


def get_registry(
    session: SessionDep, settings: SettingsDep, perms: PermsDep, upstreams: UpstreamsDep
) -> Registry:
    return Registry(session, settings, perms, upstreams)


def get_keys(session: SessionDep, perms: PermsDep) -> KeyService:
    return KeyService(session, perms)


RegistryDep = Annotated[Registry, Depends(get_registry)]
KeysDep = Annotated[KeyService, Depends(get_keys)]


def get_grants(
    registry: RegistryDep, keys: KeysDep, perms: PermsDep, access: AccessDep
) -> GrantService:
    return GrantService(registry, keys, perms, access)


GrantsDep = Annotated[GrantService, Depends(get_grants)]
