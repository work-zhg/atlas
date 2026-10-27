"""接口依赖：会话、注册表、Key、用户中心。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_session
from ..keys import KeyStore
from ..perms import Permissions, UsableCache
from ..registry import Registry
from ..settings import GatewaySettings, get_settings

__all__ = ["KeysDep", "PermsDep", "RegistryDep", "SessionDep", "SettingsDep", "UsableDep"]

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[GatewaySettings, Depends(get_settings)]


def get_perms(request: Request) -> Permissions:
    """进程内单例（app 生命周期里建好；测试注入内存实现）。"""
    return request.app.state.perms  # type: ignore[no-any-return]


def get_usable(request: Request) -> UsableCache:
    return request.app.state.usable  # type: ignore[no-any-return]


def get_registry(session: SessionDep, settings: SettingsDep) -> Registry:
    return Registry(session, settings)


def get_keys(session: SessionDep) -> KeyStore:
    return KeyStore(session)


PermsDep = Annotated[Permissions, Depends(get_perms)]
UsableDep = Annotated[UsableCache, Depends(get_usable)]
RegistryDep = Annotated[Registry, Depends(get_registry)]
KeysDep = Annotated[KeyStore, Depends(get_keys)]
