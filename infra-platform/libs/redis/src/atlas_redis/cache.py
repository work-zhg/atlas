"""防击穿协同缓存（c4-model/infra-platform/code-redis.html）。

解决授权快照、网关配置这类高频读场景的三类典型故障：
  1. 缓存击穿（惊群）：TTL 到期瞬间，按 key 经 Singleflight 只放 1 个协程回源，同键并发共享 Future；
  2. 上游瞬断 500：回源抛异常时，若存在未过 stale_ttl_s 的旧值，记告警日志并降级返旧值；
  3. 多副本延迟生效：可选接入 PubSubHub，一处 invalidate(key, broadcast=True) 立刻通知全集群驱逐；
     且收到 PubSubHub 重连后的 Resubscribed 哨兵时主动清空本地 L1，关闭断线期间漏失效窗口。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .pubsub import PubSubHub, Resubscribed

__all__ = ["CoalescedCache"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Entry[T]:
    value: T
    expires_at: float
    stale_until: float


class CoalescedCache[T]:
    """带 Singleflight、旧值降级与可选 PubSub 跨副本失效的本地 L1 缓存。"""

    def __init__(
        self,
        *,
        ttl_s: float = 5.0,
        stale_ttl_s: float = 60.0,
        max_entries: int = 10_000,
        pubsub: PubSubHub | None = None,
        invalidate_channel: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s 必须大于 0")
        if stale_ttl_s < ttl_s:
            raise ValueError("stale_ttl_s 不能小于 ttl_s")
        if max_entries <= 0:
            raise ValueError("max_entries 必须大于 0")
        self._ttl_s = ttl_s
        self._stale_ttl_s = stale_ttl_s
        self._max_entries = max_entries
        self._pubsub = pubsub
        self._channel = invalidate_channel
        self._clock = clock

        self._entries: OrderedDict[str, _Entry[T]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[T]] = {}
        self._listener: asyncio.Task[None] | None = None

    def __len__(self) -> int:
        return len(self._entries)

    async def get_or_load(self, key: str, loader: Callable[[], Awaitable[T]]) -> T:
        """取未过期缓存；过期或未命中时按 key 合并并发回源，失败在宽限期内降级返旧值。"""
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None and now < entry.expires_at:
            self._entries.move_to_end(key)
            return entry.value

        existing = self._inflight.get(key)
        if existing is not None:
            # ★ shield：单个调用方超时取消不能打断共享的回源任务，拖垮其它正在等的请求
            return await asyncio.shield(existing)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        self._inflight[key] = future
        try:
            value = await loader()
        except BaseException as exc:
            now_err = self._clock()
            if isinstance(exc, Exception) and entry is not None and now_err < entry.stale_until:
                logger.warning(
                    "CoalescedCache 回源失败，降级使用未过宽限期的旧值：key=%s err=%s",
                    key,
                    exc,
                )
                if not future.done():
                    future.set_result(entry.value)
                return entry.value
            if not future.done():
                future.set_exception(exc)
                # 防止无其它共享者 await future 时产生 "Future exception was never retrieved"
                future.exception()
            raise
        else:
            now_ok = self._clock()
            self._put(key, value, now_ok)
            if not future.done():
                future.set_result(value)
            return value
        finally:
            if self._inflight.get(key) is future:
                self._inflight.pop(key, None)

    def _put(self, key: str, value: T, now: float) -> None:
        self._entries[key] = _Entry(
            value=value,
            expires_at=now + self._ttl_s,
            stale_until=now + self._stale_ttl_s,
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    async def invalidate(self, key: str, *, broadcast: bool = True) -> None:
        """驱逐本地缓存；配置了 PubSubHub 时广播给同服务所有副本。"""
        self._entries.pop(key, None)
        if broadcast and self._pubsub is not None and self._channel is not None:
            await self._pubsub.publish(self._channel, key)

    def clear(self) -> None:
        self._entries.clear()

    def start_invalidation_listener(self) -> None:
        """启动跨副本失效监听任务（需配置 pubsub 与 invalidate_channel）。"""
        if self._pubsub is None or self._channel is None:
            return
        if self._listener is not None and not self._listener.done():
            return
        self._listener = asyncio.create_task(
            self._listen(), name=f"atlas-redis-cache-inv:{self._channel}"
        )

    async def stop_invalidation_listener(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener
            self._listener = None

    async def _listen(self) -> None:
        assert self._pubsub is not None and self._channel is not None
        async with self._pubsub.subscribe(self._channel) as queue:
            while True:
                msg = await queue.get()
                if isinstance(msg, Resubscribed):
                    # 断线期间可能漏过失效广播：安全起见整表清空，下次请求回源刷新
                    logger.info(
                        "CoalescedCache 收到重订阅哨兵，清空本地缓存：channel=%s", self._channel
                    )
                    self._entries.clear()
                else:
                    self._entries.pop(msg, None)
