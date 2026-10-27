from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..settings import get_settings

__all__ = ["get_engine", "get_session", "get_sessionmaker"]


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖（管理 / 内部接口）：一个请求一个事务，异常回滚。

    ★ 数据面不用它：转发可能持续很久（模型的流式输出），事务不能跨着转发开着 ——
      proxy.py 在转发前开一个短会话，用量记录由 usage.Recorder 另开会话异步写。
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
