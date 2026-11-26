"""调用记录分区维护：模型网关（启动与每日建下月分区）与管理平台（CallRetention 删过期分区）共用。"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["PartitionKeeper"]


class PartitionKeeper:
    """调用记录按月分区：本月和下个月的分区提前建好（启动时、每天各一次）。"""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    @staticmethod
    def _months(now: datetime) -> list[datetime]:
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return [first, nxt, (nxt + timedelta(days=32)).replace(day=1)]

    async def ensure(self, now: datetime | None = None) -> None:
        months = self._months(now or datetime.now(UTC))
        async with self._sm() as session:
            for start, end in itertools.pairwise(months):
                name = f"gw_model_call_{start:%Y%m}"
                await session.execute(
                    text(
                        f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF gw_model_call "
                        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
                    )
                )
            await session.commit()

    async def drop_before(self, cutoff: datetime) -> list[str]:
        """保留期之外的分区整块删掉。"""
        dropped: list[str] = []
        async with self._sm() as session:
            rows = await session.execute(
                text(
                    "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                    "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = 'gw_model_call'"
                )
            )
            for (name,) in rows:
                suffix = name.rsplit("_", 1)[-1]
                if suffix.isdigit() and suffix < f"{cutoff:%Y%m}":
                    await session.execute(text(f"DROP TABLE IF EXISTS {name}"))
                    dropped.append(name)
            await session.commit()
        return dropped
