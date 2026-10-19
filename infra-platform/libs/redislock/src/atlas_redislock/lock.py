"""基于 Redis 的分布式锁：单实例、owner 校验、看门狗续租。

一把锁 = 一个键，键值 = owner。拿锁是 SET NX PX，续租 / 释放都先校验 owner（Lua 原子执行）。

常规用法 —— 拿锁、续租、放锁都交给 hold()：

    lock = RedisLock(redis, f"kb:index:{doc_uuid}", ttl_s=30)
    try:
        async with lock.hold():
            await ingest(doc_uuid)      # 跑多久都行：看门狗每 ttl/3 续一次
    except LockBusy:
        ...                             # 别人在做，跳过
    except LockLost:
        ...                             # 中途失锁，body 已被取消

跨任务 / 跨实例持有（一处拿锁、另一处续租或释放）用底层原语 acquire / renew / reclaim /
release，owner 由调用方给定（如 run_id）—— 接管者用同一个 owner 就能续上。

★ TTL 不是工作时长上限：它只决定持有者进程死掉后锁空悬多久。所以 TTL 要短（默认 30s），
  长任务靠续租撑住，而不是把 TTL 调到覆盖最长的任务。

★ 这是「效率锁」，不是「正确性锁」：
  · 单 Redis 实例。主从切换时还没复制过去的锁会丢；不做 Redlock（要多个独立主节点，
    且安全性建立在时钟假设上）。
  · 持有者停顿（GC、事件循环被同步代码堵住）超过 TTL 时，锁可能已经在别人手里，
    而它要到下一次续租才知道。
  需要严格互斥的写，在资源侧再做一次 CAS / 版本号校验（本仓的做法：串行的权威在 DB）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from types import TracebackType
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

__all__ = ["LockBusy", "LockError", "LockLost", "RedisLock"]

#: 只续自己持有的（GET 与 PEXPIRE 之间不能有窗口）
_RENEW = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "  return redis.call('pexpire', KEYS[1], ARGV[2]) "
    "end return 0"
)
#: 续期，或在已过期时以同一个 owner 重新占上 —— 只要锁不在别人手里
_RECLAIM = (
    "local v = redis.call('get', KEYS[1]) "
    "if v == false or v == ARGV[1] then "
    "  redis.call('set', KEYS[1], ARGV[1], 'PX', ARGV[2]) return 1 "
    "end return 0"
)
#: 只删自己持有的（GET 与 DEL 两步之间，锁可能过期又被别人拿走）
_RELEASE = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"
)

_MIN_BACKOFF_S = 0.05
_MAX_BACKOFF_S = 1.0


class LockError(Exception):
    def __init__(self, key: str, message: str) -> None:
        super().__init__(f"{message}：{key}")
        self.key = key


class LockBusy(LockError):
    """等待期限内没拿到锁：别人持有。"""

    def __init__(self, key: str) -> None:
        super().__init__(key, "锁被占用")


class LockLost(LockError):
    """持有期间失去了锁：被别人占了，或在一个 TTL 内都没能续上。"""

    def __init__(self, key: str) -> None:
        super().__init__(key, "持有期间失去了锁")


class RedisLock:
    """★ 一个实例对应一次持有：不可重入，也不要在并发的任务之间共享同一个实例。

    redis 是 redis.asyncio 客户端，decode_responses 开不开都行。
    """

    def __init__(
        self,
        redis: Redis,
        key: str,
        *,
        ttl_s: float = 30.0,
        owner: str | None = None,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s 必须大于 0")
        self._redis = redis
        self.key = key
        self.owner = owner or uuid4().hex
        self.ttl_s = ttl_s
        self._ttl_ms = max(int(ttl_s * 1000), 1)
        #: 本地估计的「锁至少有效到」的 monotonic 时刻。从请求**发出**时算起 ——
        #: 请求在路上花的时间只会让真实过期更晚，估计偏保守
        self._valid_until = 0.0

    async def acquire(self, *, wait_s: float = 0.0) -> bool:
        """拿锁。wait_s=0 只试一次；>0 时带抖动退避重试到期限；math.inf 一直等。"""
        deadline = time.monotonic() + wait_s
        delay = _MIN_BACKOFF_S
        while True:
            sent = time.monotonic()
            if await self._redis.set(self.key, self.owner, nx=True, px=self._ttl_ms):
                self._valid_until = sent + self.ttl_s
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # 抖动：多个等待者别在同一时刻一起撞上去
            await asyncio.sleep(min(delay * random.uniform(0.5, 1.5), remaining))
            delay = min(delay * 2, _MAX_BACKOFF_S)

    async def renew(self) -> bool:
        """续一个 TTL。只续自己持有的；锁已过期或在别人手里返回 False。"""
        return await self._extend(_RENEW)

    async def reclaim(self) -> bool:
        """续一个 TTL；锁已过期就以同一个 owner 重新占上。只在别人持有时返回 False。

        给接管用：owner 是业务 id（如 run_id），接管者领到工作时原来的锁多半已经过期。
        """
        return await self._extend(_RECLAIM)

    async def release(self) -> bool:
        """放锁。只删自己持有的；返回 False 表示锁早已过期或不在自己手里。"""
        self._valid_until = 0.0
        return bool(await self._redis.eval(_RELEASE, 1, self.key, self.owner))

    async def owned(self) -> bool:
        value = await self._redis.get(self.key)
        if isinstance(value, bytes):
            value = value.decode()
        return value == self.owner

    def hold(self, *, wait_s: float = 0.0) -> _Hold:
        """`async with lock.hold():` —— 拿锁、看门狗续租、退出时放锁。

        拿不到抛 LockBusy；持有期间失锁会取消 body 所在的任务，出口处抛 LockLost。
        ★ body 不要吞掉 CancelledError，否则失锁时打断不了它。
        """
        return _Hold(self, wait_s)

    async def _extend(self, script: str) -> bool:
        sent = time.monotonic()
        ok = bool(await self._redis.eval(script, 1, self.key, self.owner, str(self._ttl_ms)))
        self._valid_until = sent + self.ttl_s if ok else 0.0
        return ok


class _Hold:
    """hold() 的上下文管理器。

    失锁时取消 body 所在的任务，在出口处把这次取消换成 LockLost —— 与 asyncio.timeout
    同一套做法：只认自己发起的那次取消，外部的取消照常透传。
    """

    def __init__(self, lock: RedisLock, wait_s: float) -> None:
        self._lock = lock
        self._wait_s = wait_s
        self._task: asyncio.Task[Any] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        #: 进入时任务已有的取消请求数；出口处 uncancel() 降回它，说明那次取消是看门狗发的
        self._cancelling = 0
        self._cancelled = False
        self._exiting = False

    async def __aenter__(self) -> RedisLock:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("hold() 必须在 asyncio 任务里使用")
        if not await self._lock.acquire(wait_s=self._wait_s):
            raise LockBusy(self._lock.key)
        self._task = task
        self._cancelling = task.cancelling()
        self._watchdog = asyncio.create_task(
            self._watch(), name=f"redislock-watchdog:{self._lock.key}"
        )
        return self._lock

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._task is not None and self._watchdog is not None
        # ★ 先于任何 await 置位：此后看门狗不会再取消本任务
        self._exiting = True
        self._watchdog.cancel()
        interrupted = False
        if self._cancelled:
            # 撤回看门狗发起的那次取消。body 已经结束、取消还没送达时，这一步也把它收回
            # （3.13 起 uncancel 归零会清掉待送达的取消），后面的 await 不会被误伤
            interrupted = (
                self._task.uncancel() <= self._cancelling
                and exc_type is not None
                and issubclass(exc_type, asyncio.CancelledError)
            )
        await asyncio.wait([self._watchdog])
        try:
            # ★ 放锁也要有上限（理由同续租）：最多等一个 TTL —— 再久锁也已自然过期
            async with asyncio.timeout(self._lock.ttl_s):
                await self._lock.release()
        except Exception:
            # 不掩盖 body 的结果：放不掉的锁 TTL 到了自然过期
            logger.warning(
                "放锁失败，%.1fs 后自然过期：%s", self._lock.ttl_s, self._lock.key, exc_info=True
            )
        if interrupted:
            raise LockLost(self._lock.key) from exc

    async def _watch(self) -> None:
        lock = self._lock
        interval = lock.ttl_s / 3
        while True:
            await asyncio.sleep(interval)
            try:
                # ★ 单次续租必须有上限：Redis 客户端可能不设 socket_timeout（本仓 redisx 就是
                #   None），断网时命令会一直挂着，而锁的有效期不会等它
                async with asyncio.timeout(max(lock._valid_until - time.monotonic(), 0)):
                    renewed = await lock.renew()
            except Exception:
                if time.monotonic() < lock._valid_until:
                    # Redis 暂时不可用：有效期内锁仍是自己的，下个节拍再试
                    logger.warning("续锁失败，稍后重试：%s", lock.key, exc_info=True)
                    continue
                renewed = False
            if not renewed:
                self._lose()
                return

    def _lose(self) -> None:
        logger.warning("失去锁 %s（被别人占用，或一个 TTL 内没能续上），中断持有者", self._lock.key)
        if self._exiting or self._task is None:
            return
        self._cancelled = True
        self._task.cancel(msg=f"lost redis lock {self._lock.key}")
