from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .db.session import get_session
from .executor.base import RunExecutor
from .identity import current_user
from .redisx import make_redis
from .services.agent import AgentService
from .services.meta import MetaService
from .services.run import RunService
from .services.thread import ThreadService

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
# 唯一的身份来源（§5.2）—— service 层不允许另取
CurrentUserDep = Annotated[UUID, Depends(current_user)]


async def get_redis(settings: SettingsDep) -> AsyncIterator[aioredis.Redis]:
    client = make_redis(settings)
    try:
        yield client
    finally:
        await client.aclose()


RedisDep = Annotated[aioredis.Redis, Depends(get_redis)]


async def get_meta_service(
    session: SessionDep, redis: RedisDep, settings: SettingsDep
) -> MetaService:
    return MetaService(session, redis, settings)


MetaServiceDep = Annotated[MetaService, Depends(get_meta_service)]


def get_executor(request: Request) -> RunExecutor:
    """执行器是**进程级单例**（持有在跑的 task），挂在 app.state 上。

    换成 arq worker 时只改这一处（§12.1）。
    """
    return request.app.state.executor  # type: ignore[no-any-return]


ExecutorDep = Annotated[RunExecutor, Depends(get_executor)]


async def get_run_service(
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    executor: ExecutorDep,
) -> RunService:
    return RunService(session, redis, settings, executor)


RunServiceDep = Annotated[RunService, Depends(get_run_service)]


async def get_agent_service(session: SessionDep) -> AgentService:
    return AgentService(session)


async def get_thread_service(session: SessionDep, settings: SettingsDep) -> ThreadService:
    return ThreadService(session, settings)


AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
ThreadServiceDep = Annotated[ThreadService, Depends(get_thread_service)]
