from __future__ import annotations

import json
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import get_settings

#: NUL 的替身：U+FFFD（替换字符），看得出这里原来有个读不出的字符
_NUL_REPLACEMENT = "\ufffd"


def _without_nul(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", _NUL_REPLACEMENT) if "\x00" in value else value
    if isinstance(value, dict):
        return {_without_nul(k): _without_nul(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_without_nul(v) for v in value]
    return value


def json_dumps(value: Any) -> str:
    """JSON 列的序列化。

    ★ PG 的 jsonb 收不下 NUL（\\u0000）：命令输出里带二进制时，整条工具结果写不进去，
      写检查点失败、整轮以「执行器内部错误」结束。在这里替换，所有 JSON 列（消息、事件、
      审批、run 状态）一处兜住。
    """
    return json.dumps(_without_nul(value))


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
        json_serializer=json_dumps,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. 一个请求一个 session，异常时回滚。"""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


#: 请求的数据库会话。★ 一律用它，不要另写 Depends(get_session)。
#:
#: 函数作用域：路由函数一返回就提交，然后才发响应。默认（request）作用域的提交在响应发出
#: 之后 —— 客户端拿到 201 立刻发下一个请求，会读不到刚写的数据（建会话后马上发消息报「会话
#: 不存在」）。代价：响应发出后 session 已关闭，流式响应的生成器里不能再用它。
#: 作用域不同的两个 Depends(get_session) 在同一请求里是两个 session，所以只能有这一处声明
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
