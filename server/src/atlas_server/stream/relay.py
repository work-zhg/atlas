"""★ Redis Stream 事件中继（文档 §10.2）。

为什么必须有它：FastAPI 多 worker 部署下，`POST /runs` 可能落在 worker A，
而浏览器的 `GET /runs/{id}/events` 落在 worker B。没有跨进程中继，B 拿不到
A 产出的事件。Redis Stream 同时解决了跨 worker 与断线重连两个问题。

读取分三种情形：
  1. Redis 里有数据      → XRANGE 补发 seq > after 的部分，再 XREAD BLOCK 续读
  2. Redis 空且 run 已终止 → 从 Postgres 的 run_event 归档表回放（超过 24h TTL 的历史）
  3. Redis 空且 run 未终止 → 直接进入 XREAD BLOCK 等待（run 刚创建，还没产出）
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from uuid import UUID

import redis.asyncio as aioredis
from atlas_server.domain.events import TERMINAL_EVENTS, EventType, TraceEvent

logger = logging.getLogger(__name__)

_TERMINAL_VALUES = {e.value for e in TERMINAL_EVENTS}


def stream_key(run_id: UUID) -> str:
    return f"run:events:{run_id}"


def cancel_key(run_id: UUID) -> str:
    return f"run:cancel:{run_id}"


def thread_lock_key(thread_id: UUID) -> str:
    return f"run:lock:{thread_id}"


def idempotency_key(key: str) -> str:
    return f"idem:run:{key}"


class EventRelay:
    def __init__(self, redis: aioredis.Redis, *, ttl_s: int = 86_400) -> None:
        self._redis = redis
        self._ttl_s = ttl_s

    async def publish(self, event: TraceEvent) -> None:
        key = stream_key(event.run_id)
        await self._redis.xadd(
            key,
            {"seq": str(event.seq), "type": event.type.value, "payload": event.model_dump_json()},
        )
        # 每次都续期：run 可能跑很久，避免中途过期
        await self._redis.expire(key, self._ttl_s)

    async def replay(self, run_id: UUID, *, after_seq: int) -> list[TraceEvent]:
        """Redis 里 seq > after_seq 的既有事件。Redis 无数据时返回空列表。"""
        entries = await self._redis.xrange(stream_key(run_id))
        out: list[TraceEvent] = []
        for _id, fields in entries:
            if int(fields["seq"]) > after_seq:
                out.append(TraceEvent.model_validate_json(fields["payload"]))
        return out

    async def has_data(self, run_id: UUID) -> bool:
        return bool(await self._redis.exists(stream_key(run_id)))

    async def tail(
        self, run_id: UUID, *, after_seq: int, block_ms: int
    ) -> AsyncIterator[TraceEvent | None]:
        """从 after_seq 之后持续产出事件；阻塞超时产出 None 供调用方发心跳。

        终止事件产出后即结束迭代。
        """
        key = stream_key(run_id)
        last_id = "0-0"
        # 先把已有的读完，同时把游标推到末尾
        entries = await self._redis.xrange(key)
        for entry_id, fields in entries:
            last_id = entry_id
            if int(fields["seq"]) > after_seq:
                event = TraceEvent.model_validate_json(fields["payload"])
                yield event
                if fields["type"] in _TERMINAL_VALUES:
                    return

        while True:
            resp = await self._redis.xread({key: last_id}, block=block_ms, count=100)
            if not resp:
                yield None  # 心跳
                continue
            for _key, entries in resp:
                for entry_id, fields in entries:
                    last_id = entry_id
                    event = TraceEvent.model_validate_json(fields["payload"])
                    yield event
                    if fields["type"] in _TERMINAL_VALUES:
                        return

    # ------------------------------------------------------------------ 取消

    async def request_cancel(self, run_id: UUID) -> None:
        await self._redis.set(cancel_key(run_id), "1", ex=3600)

    async def is_cancelled(self, run_id: UUID) -> bool:
        return bool(await self._redis.exists(cancel_key(run_id)))

    async def clear_cancel(self, run_id: UUID) -> None:
        await self._redis.delete(cancel_key(run_id))

    # ------------------------------------------------------------------ 串行锁

    #: 只删自己持有的锁（compare-and-delete 必须原子，GET+DEL 两步之间
    #: 锁可能过期又被新 run 拿走）
    _RELEASE_IF_OWNER = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end "
        "return 0"
    )

    async def acquire_thread_lock(self, thread_id: UUID, *, owner: UUID, ttl_s: int) -> bool:
        """同一会话同时只允许一个 run，防止并发写坏 state。

        锁值写 owner（= run_id）：TTL 意外过期、锁被下一个 run 拿走后，
        旧 run 结束时不能把新 run 的锁误删掉 —— 那会把「串行」的窗口
        再撕开一次。
        """
        return bool(
            await self._redis.set(thread_lock_key(thread_id), str(owner), nx=True, ex=ttl_s)
        )

    async def release_thread_lock(self, thread_id: UUID, *, owner: UUID) -> None:
        await self._redis.eval(self._RELEASE_IF_OWNER, 1, thread_lock_key(thread_id), str(owner))

    # ------------------------------------------------------------------ 幂等

    async def remember_idempotency(self, key: str, run_id: UUID, *, ttl_s: int = 86_400) -> None:
        await self._redis.set(idempotency_key(key), str(run_id), ex=ttl_s)

    async def lookup_idempotency(self, key: str) -> UUID | None:
        found = await self._redis.get(idempotency_key(key))
        return UUID(found) if found else None


class RedisCancelToken:
    """注入给 engine 的取消信号（engine 不认识 Redis，只认这个协议）。"""

    def __init__(self, relay: EventRelay, run_id: UUID) -> None:
        self._relay = relay
        self._run_id = run_id

    async def is_cancelled(self) -> bool:
        try:
            return await self._relay.is_cancelled(self._run_id)
        except Exception:  # Redis 抖动不该让整个 run 挂掉
            logger.warning("取消信号检查失败，按未取消处理", exc_info=True)
            return False


def event_from_row(*, run_id: UUID, seq: int, ts, type_: str, depth: int, data: dict) -> TraceEvent:
    """Postgres 归档行 → TraceEvent（超过 Redis TTL 的历史回放）。"""
    return TraceEvent(seq=seq, run_id=run_id, ts=ts, type=EventType(type_), depth=depth, data=data)
