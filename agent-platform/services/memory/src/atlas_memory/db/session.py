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
    """FastAPI 依赖：一个请求一个事务，异常回滚。

    ★ 实例锁（SELECT … FOR UPDATE）就挂在这个事务上：请求结束提交时才释放，所以一次写入
      （删除 + 新增、版本号分配）在锁内、在同一个事务里完成（§3.5）。
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
