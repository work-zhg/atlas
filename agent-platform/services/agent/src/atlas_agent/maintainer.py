"""维护者：全局只需要一个实例做的周期任务（横向扩展设计 §7.1）。

每个执行器都参与竞选（Redis SET NX PX + 续租），持有者定期执行，进程退出后别的实例接任：

    孤儿 Pod 回收     每 pod_reap_interval_s（原来每个副本启动时各跑一遍）
    记忆处理中回收    所属实例已不在的处理中列表搬回主队列（§7.2）
    记忆死信复活      按轮次重新入队，有上限

另外每个实例都写心跳（atlas:instance:{id}）—— 「所属实例还在不在」靠它判断。

★ 挂起 run 的补唤醒**不在这里**：它靠 CAS 幂等，每个执行器都扫，多扫几遍只是多几次查询。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from .instance import instance_id
from .memory.queue import recover_orphaned, revive_dead
from .redisx import make_redis
from .repositories.thread import ThreadRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from .config import Settings

logger = logging.getLogger(__name__)

__all__ = ["Maintainer"]

LEADER_KEY = "atlas:maintainer"
INSTANCE_PREFIX = "atlas:instance:"
_MEMORY_RECOVER_S = 60.0
_MEMORY_REVIVE_S = 1800.0

#: 只续自己持有的（GET 与 PEXPIRE 之间不能有窗口）
_RENEW = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "  return redis.call('pexpire', KEYS[1], ARGV[2]) "
    "end return 0"
)
_RELEASE = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"
)


class Maintainer:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        pods: Any,
        *,
        memory_enabled: bool,
    ) -> None:
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._pods = pods
        self._memory_enabled = memory_enabled
        self._me = instance_id()
        self._lease_ms = int(settings.maintainer_lease_s * 1000)
        self._leader = False
        #: 任务名 → 下次到期的 monotonic 时刻。成为维护者时才排上
        self._due: dict[str, float] = {}

    async def run_forever(self) -> None:
        tick = max(self._settings.maintainer_lease_s / 3, 0.5)
        while True:
            redis = make_redis(self._settings)
            try:
                await redis.set(INSTANCE_PREFIX + self._me, "1", px=self._lease_ms)
                await self._elect(redis)
                if self._leader:
                    await self._run_due(redis)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("维护者这一轮失败", exc_info=True)
            finally:
                with contextlib.suppress(Exception):
                    await redis.aclose()
            await asyncio.sleep(tick)

    async def release(self) -> None:
        """停机时让位，别的实例下一个节拍就能接任（而不是等租约过期）。"""
        redis = make_redis(self._settings)
        try:
            await redis.eval(_RELEASE, 1, LEADER_KEY, self._me)
            await redis.delete(INSTANCE_PREFIX + self._me)
        except Exception:
            logger.warning("维护者让位失败", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()

    async def _elect(self, redis: Any) -> None:
        held = bool(await redis.eval(_RENEW, 1, LEADER_KEY, self._me, str(self._lease_ms)))
        if not held:
            held = bool(await redis.set(LEADER_KEY, self._me, nx=True, px=self._lease_ms))
        if held and not self._leader:
            logger.info("本实例成为维护者（%s）", self._me)
            now = time.monotonic()
            # ★ Pod 回收不在接任的那一刻做：刚起来的集群里会话正在建 Pod，等一个周期再说
            self._due = {
                "pods": now + self._settings.pod_reap_interval_s,
                "memory_recover": now,
                "memory_revive": now + _MEMORY_REVIVE_S,
            }
        elif not held and self._leader:
            logger.info("本实例不再是维护者")
        self._leader = held

    async def _run_due(self, redis: Any) -> None:
        now = time.monotonic()
        if now >= self._due.get("pods", now + 1):
            self._due["pods"] = now + self._settings.pod_reap_interval_s
            await self._reap_pods()
        if self._memory_enabled and now >= self._due.get("memory_recover", now + 1):
            self._due["memory_recover"] = now + _MEMORY_RECOVER_S
            await recover_orphaned(redis, await self._live_instances(redis))
        if self._memory_enabled and now >= self._due.get("memory_revive", now + 1):
            self._due["memory_revive"] = now + _MEMORY_REVIVE_S
            await revive_dead(redis)

    async def _reap_pods(self) -> None:
        """把还活着的会话交给 cluster，其余的它自己清。

        ★ 名单是**取完马上用**的：取名单与回收之间新建的会话，其 Pod 还很年轻，
          cluster 只回收存在超过保留期的孤儿（reap_min_age_s），不会误删。
        """
        async with self._sessionmaker() as session:
            live = await ThreadRepository(session).all_ids()
        await self._pods.reap([str(thread_id) for thread_id in live])

    @staticmethod
    async def _live_instances(redis: Any) -> set[str]:
        live: set[str] = set()
        async for key in redis.scan_iter(match=f"{INSTANCE_PREFIX}*"):
            name = key.decode() if isinstance(key, bytes) else str(key)
            live.add(name.removeprefix(INSTANCE_PREFIX))
        return live
