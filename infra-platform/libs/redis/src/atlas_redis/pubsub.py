"""单连接多路复用 PubSub 总线（c4-model/infra-platform/code-redis.html）。

★ 为什么必须单连接多路复用：
  CLI 审批等待、授权快照失效、租户停用监听、流程唤醒都依赖 PubSub。如果每个等待协程各开一条
  redis.pubsub() 连接，500 个在等审批的会话就会长期独占 500 条短命令池连接。
  PubSubHub 在整个进程内只维护 **1 条** 专用订阅连接，按 channel 路由到各订阅者的有界队列。

★ 断线自愈与 Resubscribed 哨兵：
  Redis PubSub 断线期间不保留离线消息。连接闪断后，PubSubHub 按指数退避自动重连并一次性
  重新 SUBSCRIBE 全部活跃 channel，同时向每个在听订阅者投递 Resubscribed 哨兵 —— 收到哨兵
  后，业务监听方应回库核对一次最新状态（或清空本地缓存），关闭断线期间漏信号窗口。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from .client import RedisClient

__all__ = ["PubSubHub", "Resubscribed"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Resubscribed:
    """断线重连并重新订阅完成后投递的哨兵：提示监听方回库补查一次状态。"""

    channel: str


class PubSubHub:
    """进程级单连接 PubSub 多路复用器。"""

    def __init__(
        self,
        *,
        commands: RedisClient,
        pubsub_factory: Callable[[], Any],
        queue_maxsize: int = 256,
        backoff_min_s: float = 0.2,
        backoff_max_s: float = 5.0,
    ) -> None:
        self._commands = commands.with_policy(fail_open=True)
        self._pubsub_factory = pubsub_factory
        self._queue_maxsize = queue_maxsize
        self._backoff_min_s = backoff_min_s
        self._backoff_max_s = backoff_max_s

        self._subs: dict[str, set[asyncio.Queue[str | Resubscribed]]] = {}
        self._pubsub: Any | None = None
        self._lock = asyncio.Lock()
        self._has_subs = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._connected = False
        self.reconnect_count = 0

    @property
    def active_channels(self) -> int:
        return len(self._subs)

    @property
    def active_subscribers(self) -> int:
        return sum(len(qs) for qs in self._subs.values())

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        """启动后台订阅循环（幂等）。"""
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run(), name="atlas-redis-pubsub-hub")

    async def stop(self) -> None:
        """停止后台订阅循环并关闭专用连接。"""
        self._stopped = True
        self._has_subs.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        await self._close_pubsub()

    async def publish(self, channel: str, payload: str, *, key_pattern: str = "") -> int:
        """发一条通知（Fail-Open：发不出去只记告警，等待方靠哨兵或低频回库兜底）。"""
        res = await self._commands.publish(
            channel, payload, key_pattern=key_pattern or "pubsub:publish"
        )
        return int(res) if isinstance(res, int) else 0

    @contextlib.asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[asyncio.Queue[str | Resubscribed]]:
        """订阅一个频道，拿到本地有界队列；退出上下文自动退订。"""
        queue: asyncio.Queue[str | Resubscribed] = asyncio.Queue(maxsize=self._queue_maxsize)
        first_for_channel = False
        async with self._lock:
            bucket = self._subs.get(channel)
            if bucket is None:
                bucket = set()
                self._subs[channel] = bucket
                first_for_channel = True
            bucket.add(queue)
            self._has_subs.set()
            if first_for_channel and self._pubsub is not None and self._connected:
                with contextlib.suppress(Exception):
                    await self._pubsub.subscribe(channel)

        # 未显式调用 start() 时按需自启，避免调用方忘记 start() 导致死等
        if self._task is None or self._task.done():
            self.start()

        try:
            yield queue
        finally:
            last_for_channel = False
            async with self._lock:
                bucket = self._subs.get(channel)
                if bucket is not None:
                    bucket.discard(queue)
                    if not bucket:
                        self._subs.pop(channel, None)
                        last_for_channel = True
                if not self._subs:
                    self._has_subs.clear()
                if last_for_channel and self._pubsub is not None and self._connected:
                    with contextlib.suppress(Exception):
                        await self._pubsub.unsubscribe(channel)

    async def iter_messages(self, channel: str) -> AsyncIterator[str | Resubscribed]:
        """便捷异步迭代器封装。"""
        async with self.subscribe(channel) as queue:
            while True:
                yield await queue.get()

    # ------------------------------------------------------------------ 内部循环

    def _deliver(self, channel: str, item: str | Resubscribed) -> None:
        for queue in list(self._subs.get(channel, ())):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                logger.warning("PubSub 慢消费者队列已满，丢弃最旧消息：channel=%s", channel)
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(item)

    async def _close_pubsub(self) -> None:
        self._connected = False
        ps = self._pubsub
        self._pubsub = None
        if ps is not None:
            with contextlib.suppress(Exception):
                await ps.aclose()

    async def _run(self) -> None:
        backoff = self._backoff_min_s
        had_connected_before = False
        while not self._stopped:
            if not self._subs:
                await self._has_subs.wait()
                if self._stopped:
                    break
            try:
                ps = self._pubsub_factory()
                async with self._lock:
                    self._pubsub = ps
                    channels = list(self._subs.keys())
                    if channels:
                        await ps.subscribe(*channels)
                    self._connected = True

                if had_connected_before:
                    self.reconnect_count += 1
                    logger.info("PubSubHub 重连成功，已重订阅 %d 个频道", len(channels))
                    for ch in channels:
                        self._deliver(ch, Resubscribed(channel=ch))
                had_connected_before = True
                backoff = self._backoff_min_s

                while not self._stopped:
                    if not self._subs:
                        await asyncio.sleep(0.05)
                        continue
                    message = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message is None:
                        continue
                    if message.get("type") != "message":
                        continue
                    raw_ch = message.get("channel")
                    raw_data = message.get("data")
                    ch_str = raw_ch.decode() if isinstance(raw_ch, bytes) else str(raw_ch)
                    data_str = raw_data.decode() if isinstance(raw_data, bytes) else str(raw_data)
                    self._deliver(ch_str, data_str)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("PubSubHub 连接异常，%.2fs 后重连：%s", backoff, exc)
                await self._close_pubsub()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, self._backoff_max_s)
        await self._close_pubsub()
