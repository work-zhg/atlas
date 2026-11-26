"""入库增强的结果缓存（kb_enhance_cache）：重跑入库不用再调模型。

    EnhanceCache      接口：get_many（开始前一次取回全部命中的）、put（每次调用成功后存一条）
    DbEnhanceCache    存在知识库的库里，带租户（当前租户，自动过滤）
    cleanup()         删掉超过保留期的（jobs.EnhanceCacheCleanup 每天在系统范围里调一次）

★ 放数据库而不是 Redis：读得少、对延迟不敏感（一次 LLM 调用要几秒），却要留 30 天、随入库量增长；
  平台的 Redis 是全平台共用的一个实例，还装着任务队列和锁，撑满了大家一起停。
★ 每次调用成功就存一条（而不是增强阶段结束时一次存）：worker 中途被杀，已经做完的部分不会丢。
★ 增强是并发的（asyncio.gather）：不能共用一个会话 —— 开始前一条 IN 查询取回命中的，每次 put
  用自己的短事务；并发的同一项已经写了就算了（唯一约束）。
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from atlas_tenancy import SystemReason, TenantScope
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from ..db.models import EnhanceCacheEntry
from ..db.types import utcnow

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["RETENTION", "DbEnhanceCache", "EnhanceCache", "cleanup"]

#: 保留多久。worker 被杀后的重跑在几分钟内；重新分段可能隔几周才做
RETENTION = timedelta(days=30)


class EnhanceCache(Protocol):
    async def get_many(self, keys: Iterable[str]) -> dict[str, str]: ...

    async def put(self, key: str, value: str) -> None: ...


class DbEnhanceCache:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_many(self, keys: Iterable[str]) -> dict[str, str]:
        wanted = list(dict.fromkeys(keys))
        if not wanted:
            return {}
        cutoff = utcnow() - RETENTION
        async with self._sessions() as s:
            rows = await s.execute(
                select(EnhanceCacheEntry.cache_key, EnhanceCacheEntry.value).where(
                    EnhanceCacheEntry.cache_key.in_(wanted),
                    EnhanceCacheEntry.created_at >= cutoff,
                )
            )
            return {k: v for k, v in rows}

    async def put(self, key: str, value: str) -> None:
        async with self._sessions() as s:
            s.add(EnhanceCacheEntry(cache_key=key, value=value))
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()  # 同一项刚被并发的调用写了，结果一样


async def cleanup(sessions: async_sessionmaker[AsyncSession]) -> int:
    """删掉超过保留期的（全部租户，系统范围）。→ 删了几条。"""
    cutoff = utcnow() - RETENTION
    with TenantScope.system(SystemReason.SWEEP):
        async with sessions() as s:
            result = await s.execute(
                delete(EnhanceCacheEntry).where(EnhanceCacheEntry.created_at < cutoff)
            )
            await s.commit()
    return int(getattr(result, "rowcount", 0) or 0)
