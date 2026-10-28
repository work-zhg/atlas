"""接口依赖：会话、LiteLLM、用户中心、模型、Key。

★ 会话用 scope="function"：提交在响应发出之前（默认的 request 作用域在响应之后才提交，
  写完立刻读的调用方会读不到）。本服务没有流式响应，不存在会话被提前关掉的问题。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_session
from ..keys import KeyService
from ..litellm_api import LiteLLM
from ..perms import Permissions, SubjectsCache
from ..registry import DeploymentsCache, ModelRegistry
from ..settings import GatewaySettings, get_settings

__all__ = [
    "KeysDep",
    "LiteLLMDep",
    "PermsDep",
    "RegistryDep",
    "SessionDep",
    "SettingsDep",
]

SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
SettingsDep = Annotated[GatewaySettings, Depends(get_settings)]


def get_perms(request: Request) -> Permissions:
    """进程内单例（app 生命周期里建好；测试注入内存实现）。"""
    return request.app.state.perms  # type: ignore[no-any-return]


def get_subjects(request: Request) -> SubjectsCache:
    return request.app.state.subjects  # type: ignore[no-any-return]


def get_litellm(request: Request) -> LiteLLM:
    return request.app.state.litellm  # type: ignore[no-any-return]


def get_deployments(request: Request) -> DeploymentsCache:
    return request.app.state.deployments  # type: ignore[no-any-return]


PermsDep = Annotated[Permissions, Depends(get_perms)]
LiteLLMDep = Annotated[LiteLLM, Depends(get_litellm)]


def get_registry(
    session: SessionDep,
    litellm: LiteLLMDep,
    deployments: Annotated[DeploymentsCache, Depends(get_deployments)],
    perms: PermsDep,
) -> ModelRegistry:
    return ModelRegistry(session, litellm, deployments, perms)


RegistryDep = Annotated[ModelRegistry, Depends(get_registry)]


def get_keys(
    session: SessionDep,
    litellm: LiteLLMDep,
    registry: RegistryDep,
    subjects: Annotated[SubjectsCache, Depends(get_subjects)],
) -> KeyService:
    return KeyService(session, litellm, registry, subjects)


KeysDep = Annotated[KeyService, Depends(get_keys)]
