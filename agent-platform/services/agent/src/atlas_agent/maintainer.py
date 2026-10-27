"""维护者：全局只需要一个实例做的周期任务（横向扩展设计 §7.1）。

每个执行器都参与竞选（Redis SET NX PX + 续租），持有者定期执行，进程退出后别的实例接任：

    孤儿沙箱回收     每 pod_reap_interval_s：会话已不存在或已归档的 ACP Pod 与原生沙箱
    沙箱闲置回收     每分钟：没有运行中的命令且闲置超过 sandbox_idle_ttl_s 的原生沙箱
                     （先删路由再删沙箱）
    会话后补记       每 memory_backfill_scan_s：空闲 / 归档的会话补上漏记的记忆、拆小超限的主题
                     （doc/memory-design.html §4.5）。★ 另起任务跑：一批要调几次 LLM，不能挡住
                     上面几项；上一批没跑完就不起下一批

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
from .providers.opensandbox import AcpProvisioner, SandboxProvisioner
from .redisx import make_redis
from .repositories.thread import ThreadRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from .config import Settings
    from .memory import Backfiller

logger = logging.getLogger(__name__)

__all__ = ["Maintainer"]

LEADER_KEY = "atlas:maintainer"
INSTANCE_PREFIX = "atlas:instance:"
_SANDBOX_IDLE_S = 60.0

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
        *,
        backfiller: Backfiller | None = None,
    ) -> None:
        self._settings = settings
        self._sessionmaker = sessionmaker
        #: 会话后补记。None = 没接入记忆服务
        self._backfiller = backfiller
        self._backfill_task: asyncio.Task[int] | None = None
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
                "sandbox_idle": now + _SANDBOX_IDLE_S,
                "memory_backfill": now + self._settings.memory_backfill_scan_s,
            }
        elif not held and self._leader:
            logger.info("本实例不再是维护者")
        self._leader = held

    async def _run_due(self, redis: Any) -> None:
        now = time.monotonic()
        if now >= self._due.get("pods", now + 1):
            self._due["pods"] = now + self._settings.pod_reap_interval_s
            await self._reap_pods(redis)
        if self._settings.sandbox_enabled and now >= self._due.get("sandbox_idle", now + 1):
            self._due["sandbox_idle"] = now + _SANDBOX_IDLE_S
            await SandboxProvisioner(self._settings, redis).reap_idle()
        if self._backfiller is not None and now >= self._due.get("memory_backfill", now + 1):
            self._due["memory_backfill"] = now + self._settings.memory_backfill_scan_s
            if self._backfill_task is None or self._backfill_task.done():
                self._backfill_task = asyncio.create_task(
                    self._backfiller.run_due(), name="memory-backfill"
                )

    async def stop(self) -> None:
        """停机：取消正在跑的补记（进度只在一个会话处理完后推进，取消不会丢东西）。"""
        task = self._backfill_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _reap_pods(self, redis: Any) -> None:
        """会话已不存在（或已归档）的 ACP Pod 与原生沙箱一律删掉。

        ★ 名单是**取完马上用**的：取名单与回收之间新建的会话，其沙箱还很年轻，
          只回收建好超过 sandbox_reap_min_age_s 的，不会误删。
        ★ 归档的会话不算活（ThreadRepository.live_ids）：task 的 fresh=true 每丢一次子会话，
          就会留下一个再也用不上的 ACP Pod。
        """
        async with self._sessionmaker() as session:
            live = {str(thread_id) for thread_id in await ThreadRepository(session).live_ids()}
        try:
            await AcpProvisioner(self._settings, redis).reap_orphans(live)
        except Exception:
            logger.warning("ACP Pod 孤儿回收失败", exc_info=True)
        if self._settings.sandbox_enabled:
            await SandboxProvisioner(self._settings, redis).reap_orphans(live)
