"""ActivityLedger：每个沙箱的运行中命令和最近活动时间，存在 Redis，按 sandbox_uuid 分键。

★ 按沙箱记：沙箱重建后 uuid 变了，自然是一本新账；以后共享沙箱，也是按沙箱判断闲不闲。
★ 多个副本共用一本账，所以放 Redis；每次提交、命令结束、打开文件都要写，放数据库不划算。
★ 键在每次写入和确认时续期（7 天）：沙箱删了就没人再写，键自然过期，不用谁来清（依赖方向是
  执行 → 生命周期，删沙箱时没人通知这里）。代价是 Redis 丢数据后，没人跟随的长命令会被当成闲置。
★ 划掉残留记录不算活动；没有活动记录时（刚建、或 Redis 丢了），从第一次查询时开始计时 ——
  只会让回收变晚，不会变早。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    import redis.asyncio as aioredis

__all__ = ["ActivityLedger"]

#: 键的寿命：每次写入、确认时续期
TTL_S = 7 * 24 * 3600


class ActivityLedger:
    def __init__(self, redis: aioredis.Redis, *, prefix: str = "atlas:sandbox") -> None:
        self._r = redis
        self._prefix = prefix

    def _running(self, sandbox_uuid: UUID) -> str:
        return f"{self._prefix}:{sandbox_uuid}:running"

    def _active(self, sandbox_uuid: UUID) -> str:
        return f"{self._prefix}:{sandbox_uuid}:active"

    async def started(self, sandbox_uuid: UUID, cmd_id: str) -> None:
        key = self._running(sandbox_uuid)
        await self._r.sadd(key, cmd_id)  # type: ignore[misc]
        await self._r.expire(key, TTL_S)
        await self.touch(sandbox_uuid)

    async def finished(self, sandbox_uuid: UUID, cmd_id: str) -> None:
        """命令结束：划掉，并算一次活动。"""
        await self._r.srem(self._running(sandbox_uuid), cmd_id)  # type: ignore[misc]
        await self.touch(sandbox_uuid)

    async def drop(self, sandbox_uuid: UUID, cmd_id: str) -> None:
        """划掉一条残留记录（确认时发现它已经不在跑）。

        ★ 不算活动：否则回收一次就把闲置计时清零了。
        """
        await self._r.srem(self._running(sandbox_uuid), cmd_id)  # type: ignore[misc]

    async def running(self, sandbox_uuid: UUID) -> set[str]:
        key = self._running(sandbox_uuid)
        members = await self._r.smembers(key)  # type: ignore[misc]
        if members:
            await self._r.expire(key, TTL_S)
        return {m.decode() if isinstance(m, bytes) else str(m) for m in members}

    async def touch(self, sandbox_uuid: UUID) -> None:
        await self._r.set(self._active(sandbox_uuid), _now().timestamp(), ex=TTL_S)

    async def last_active(self, sandbox_uuid: UUID) -> datetime:
        key = self._active(sandbox_uuid)
        # 没有记录时从这一刻开始计时（NX：不覆盖别人刚写的）
        await self._r.set(key, _now().timestamp(), ex=TTL_S, nx=True)
        raw = await self._r.get(key)
        await self._r.expire(key, TTL_S)
        try:
            return datetime.fromtimestamp(float(raw), UTC)
        except (TypeError, ValueError):
            return _now()


def _now() -> datetime:
    return datetime.now(UTC)
