"""决定信号（c4-model/agent/code-approval.html · DecisionChannel）。

给在线等待用：CLI 智能体的权限请求在线等用户决定（进程在跑、bridge 在等），不能像原生那样挂起。
做了决定就在 Redis 上发一个信号，等待方收到就去库里取结果。

★ 信号只是「该去看看了」，结果永远以库里为准：信号可能丢（订阅之前发的、连接抖了），等待方
  按调用方给的间隔低频回库兜底。处理决定请求的实例和连着 bridge 的实例可以不是同一个。
★ 生产路径经 atlas_redis.PubSubHub 单连接多路复用：无论多少个 CLI 会话同时在等审批，
  进程内永远只占 1 条专用订阅连接，绝不挤压 commands 短命令池；断线重连收到 Resubscribed
  哨兵时立刻唤醒等待方回库核对一次。测试传假 Redis 时自动回退到传统 pubsub() 路径。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from uuid import UUID

from atlas_redis import PubSubHub, Resubscribed

if TYPE_CHECKING:
    from redis.asyncio.client import PubSub

__all__ = ["DecisionChannel", "DecisionSignal"]

logger = logging.getLogger(__name__)


def _key(approval_id: UUID | str) -> str:
    return f"atlas:approval:decided:{approval_id}"


class DecisionSignal:
    """一次订阅：wait 等到信号（或断线重连哨兵）返回 True，到点没等到返回 False。"""

    def __init__(
        self,
        pubsub: PubSub | None = None,
        *,
        queue: asyncio.Queue[str | Resubscribed] | None = None,
    ) -> None:
        self._pubsub = pubsub
        self._queue = queue

    async def wait(self, timeout: float) -> bool:
        if self._queue is not None:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except TimeoutError:
                return False
            # 收到真实决定信号或 PubSubHub 重连哨兵都唤醒调用方回库核对
            return item is not None

        assert self._pubsub is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (left := deadline - loop.time()) > 0:
            message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=left)
            if message is not None and message.get("type") == "message":
                return True
        return False


class DecisionChannel:
    def __init__(self, redis: Any, *, pubsub: PubSubHub | None = None) -> None:
        self._r = redis
        self._pubsub = pubsub

    async def publish(self, approval_id: UUID | str) -> None:
        """做了决定：叫醒在等这件事的。发不出去只记日志 —— 等待方会回库兜底。"""
        try:
            if self._pubsub is not None:
                await self._pubsub.publish(_key(approval_id), "decided")
            else:
                await self._r.publish(_key(approval_id), "decided")
        except Exception:
            logger.warning("审批 %s 的决定信号没发出去", approval_id, exc_info=True)

    @contextlib.asynccontextmanager
    async def listen(self, approval_id: UUID | str) -> AsyncIterator[DecisionSignal]:
        """先订阅、再让调用方回库看状态、然后等 —— 订阅在前，看完状态之后发的信号不会漏。"""
        if self._pubsub is not None:
            async with self._pubsub.subscribe(_key(approval_id)) as queue:
                yield DecisionSignal(queue=queue)
            return

        raw = getattr(self._r, "raw", self._r)
        pubsub = raw.pubsub()
        await pubsub.subscribe(_key(approval_id))
        try:
            yield DecisionSignal(pubsub)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe()
                await pubsub.aclose()
