"""接口依赖：会话、目录、Key、用户中心。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_session
from ..keys import KeyStore
from ..perms import Permissions, SubjectsCache
from ..registry import Registry
from ..settings import GatewaySettings, get_settings
from ..usage import Recorder

__all__ = [
    "KeysDep",
    "PermsDep",
    "RecorderDep",
    "RegistryDep",
    "SessionDep",
    "SettingsDep",
    "SubjectsDep",
]

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[GatewaySettings, Depends(get_settings)]


def get_perms(request: Request) -> Permissions:
    """进程内单例（app 生命周期里建好；测试注入内存实现）。"""
    return request.app.state.perms  # type: ignore[no-any-return]


def get_subjects(request: Request) -> SubjectsCache:
    return request.app.state.subjects  # type: ignore[no-any-return]


def get_recorder(request: Request) -> Recorder:
    return request.app.state.recorder  # type: ignore[no-any-return]


def get_registry(session: SessionDep, settings: SettingsDep) -> Registry:
    return Registry(session, settings)


def get_keys(session: SessionDep) -> KeyStore:
    return KeyStore(session)


PermsDep = Annotated[Permissions, Depends(get_perms)]
SubjectsDep = Annotated[SubjectsCache, Depends(get_subjects)]
RecorderDep = Annotated[Recorder, Depends(get_recorder)]
RegistryDep = Annotated[Registry, Depends(get_registry)]
KeysDep = Annotated[KeyStore, Depends(get_keys)]
