"""Run 执行器 · 调度与租约（c4-model/agent/code-executor-scheduling.html）。

这个 run 由哪个实例来跑。run 是数据库里的工作，建好时就是排队状态：

    submit    → 只发唤醒（run 行在建的时候就是 queued）
    领取      → CAS + 租约（RunQueue.claim_next），本实例并发满了就不领（背压）
    续租      → 每 lease/3 一条 SQL 续本实例的全部 run；续不上 = 被接管了，立即停
    交接      → 停机时 native 走到安全点、acp 直接断开，清租约让别的实例立刻领走

一段怎么跑、怎么落库归「一段的执行」（segment.py）；挂起的 run 什么时候续、接管时怎么
收拾现场归「续跑、接管与取消」（continuation.py）。运行时由组合根装配好注入（compose.py），
这里不认识任何一种运行时。

★ 不设到点结束的超时：租约是「进程还活着」的心跳，不是工作时长上限。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..redisx import make_redis
from ..repositories.run import SUSPENDED_STATUSES, LeaseLost, RunRepository
from ..repositories.run_queue import RunQueue
from ..stream.relay import RUN_WAKE_CHANNEL, EventRelay
from .continuation import Continuation
from .segment import SegmentExecution

logger = logging.getLogger(__name__)


@dataclass
class ExecutionSlot:
    """本实例上一个在跑的 run（实现 segment.SegmentControl）。"""

    task: asyncio.Task[None] | None = None
    thread_id: UUID | None = None
    #: 收尾已开始落库：不再续租、续不上也不算「被接管」（终态/挂起会清掉租约）
    settled: bool = False
    #: 租约已不在本实例（被别的实例接管）：什么都不能再写，锁也不能放
    lost: bool = False
    #: 停机中：native 到安全点就交接
    drain: bool = False
    #: CLI 智能体：停机时直接断开（那一轮在 Pod 里照常进行）
    cli: bool = False


class RunWorker:
    """一个实例上的执行器，实现 RunExecutor：唤醒加定时扫描，在并发上限内 CAS 领取，
    领到就起一个执行槽交给「一段的执行」，有 run 在跑时续租，停机时交接。
    api 角色的实例只入队、不领取。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        instance: str,
        segment: SegmentExecution,
        continuation: Continuation,
        model_builder: Any = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._segment = segment
        self._continuation = continuation
        #: 组合根用的模型构造器（手动压缩等 API 侧的模型调用用它，测试可注入）
        self._build_model = model_builder
        #: 租约的 owner
        self._instance = instance
        #: api 角色只入队、不执行（横向扩展设计 §3）
        self._executes = settings.server_role != "api"
        self._slots: dict[UUID, ExecutionSlot] = {}
        #: 领取突发（submit / 唤醒 / 扫描触发）的任务引用 —— asyncio 只持弱引用
        self._kicks: set[asyncio.Task[None]] = set()
        self._claim_lock = asyncio.Lock()
        #: 周期任务：补唤醒扫描、领取扫描、唤醒订阅
        self._loops: list[asyncio.Task[None]] = []
        #: 续租任务。有 run 在跑才需要，按需起
        self._renewer: asyncio.Task[None] | None = None
        self._draining = False
        self._drain_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ 协议

    @property
    def model_builder(self) -> Any:
        """与执行器同一个模型构造器（手动压缩等 API 侧的模型调用用它，测试可注入）。"""
        return self._build_model

    @property
    def draining(self) -> bool:
        return self._draining

    async def submit(self, run_id: UUID) -> None:
        """run 行已是 queued：广播唤醒，本实例也立刻试着领一次。

        ★ 不再「在接到请求的进程里起任务」：领到它的可能是任何一个实例（§4.2）。
          本实例若是执行器且有空位，通常就是它自己 —— 与原来的延迟相当。
        """
        await self._wake()
        self._kick()

    async def cancel(self, run_id: UUID) -> None:
        redis = self._redis()
        try:
            await EventRelay(redis).request_cancel(run_id)
        finally:
            await redis.aclose()

        # ★ 挂起中的 run **没有进程**会读到这个取消位。
        #
        #   别处的取消都是「设位，让正在跑的循环自己看见」——  runner 每步
        #   检查一次，到安全点产出 run.cancelled 然后走正常收尾。suspended
        #   没有那个循环：不当场收的话，这个 run 会一直躺着，会话也一直被
        #   它占着（用户连下一句都发不出），直到某个子 run 恰好跑完才被
        #   resume_if_ready 顺手发现。而子 run 可能要跑一小时。
        #
        #   ★ 不等子 run。用户要的是**现在**停下；子 run 已经被级联取消
        #     （RunService.cancel），它自己会收自己的尾。
        async with self._sessionmaker() as session:
            run = await RunRepository(session).get(run_id)
        if run is not None and run.status in SUSPENDED_STATUSES:
            await self._continuation.settle_cancelled(run_id)

    async def resume_if_ready(self, run_id: UUID) -> bool:
        """等的东西都到了就把这个挂起的 run 续上（判据在续跑领域）。"""
        return await self._continuation.resume_if_ready(run_id)

    async def decided(self, run_id: UUID, approval_id: UUID) -> None:
        """审批组件的 DecisionListener：做了决定就查一次续跑条件。"""
        await self._continuation.decided(run_id, approval_id)

    async def shutdown(self) -> None:
        """停机：先交接在跑的 run（§6），再停周期任务。"""
        await self.drain()
        for task in [*self._loops, *self._kicks]:
            task.cancel()
        for task in [*self._loops, *self._kicks]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._loops.clear()
        self._kicks.clear()

    # ------------------------------------------------------------------ 周期任务

    def start(self) -> None:
        """起本实例的周期任务。api 角色什么都不起 —— 它不执行 run。

        补唤醒扫描   丢了唤醒信号的挂起 run 只有它能捞回来（CAS 幂等，每个执行器都扫）
        领取扫描     唤醒丢了也不丢 run；接管租约过期的 run 也靠它
        唤醒订阅     新 run 入队后秒级领取，不必等扫描
        """
        if self._loops or not self._executes:
            return
        self._loops = [
            asyncio.create_task(self._sweep_forever(), name="suspended-sweeper"),
            asyncio.create_task(self._claim_forever(), name="run-claimer"),
            asyncio.create_task(self._listen_wakes(), name="run-wake-listener"),
        ]

    async def _sweep_forever(self) -> None:
        """把丢了唤醒信号的 suspended run 捞回来。

        ★ 为什么不能只靠子 run 主动唤醒：那个唤醒发生在子 run 收尾的代码里，
          而进程可能正好在那一刻被 SIGKILL。信号没了之后**没有任何其它机制**
          会再碰这个父 run。这个扫描是它唯一的出路。

        ★ 幂等靠续跑领域的 CAS：扫描与主动唤醒撞上时只有一个能赢。
        """
        interval = self._settings.suspended_sweep_interval_s
        while True:
            await asyncio.sleep(interval)
            try:
                await self.sweep_suspended()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 扫描失败只是这一轮没捞到，下一轮还会来 —— 绝不能让它把
                # 循环带走，否则「唯一的出路」就没了。
                logger.warning("suspended 扫描失败", exc_info=True)

    async def sweep_suspended(self) -> int:
        """扫一遍挂起的 run，能续的续上。返回续了几个（判据在续跑领域）。"""
        return await self._continuation.sweep_suspended()

    async def _claim_forever(self) -> None:
        while True:
            await self.claim_available()
            await asyncio.sleep(self._settings.run_claim_scan_s)

    async def _listen_wakes(self) -> None:
        """订阅唤醒广播。断了就退避重连 —— 它只是加速，断着也有扫描兜底。"""
        delay = 1.0
        while True:
            redis = self._redis()
            try:
                pubsub = redis.pubsub()
                await pubsub.subscribe(RUN_WAKE_CHANNEL)
                delay = 1.0
                async for message in pubsub.listen():
                    if message.get("type") == "message":
                        self._kick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("唤醒订阅断开，%.0fs 后重连", delay, exc_info=True)
            finally:
                with contextlib.suppress(Exception):
                    await redis.aclose()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _wake(self) -> None:
        redis = self._redis()
        try:
            await redis.publish(RUN_WAKE_CHANNEL, "1")
        except Exception:  # 唤醒丢了只是慢一点：扫描会领到它
            logger.warning("发布 run 唤醒失败", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()

    # ------------------------------------------------------------------ 领取

    def _kick(self) -> None:
        """在后台试着领取。★ 全新的 contextvars.Context，理由见 _spawn。"""
        if not self._executes or self._draining:
            return
        task = asyncio.create_task(
            self.claim_available(), name="run-claim", context=contextvars.Context()
        )
        self._kicks.add(task)
        task.add_done_callback(self._kicks.discard)

    async def claim_available(self) -> int:
        """领到本实例的并发上限为止。返回领了几个。

        ★ 串行领取（锁）：并发的几次 kick 各自领的话，会一起冲过并发上限。
        """
        claimed = 0
        async with self._claim_lock:
            while not self._draining and len(self._slots) < self._settings.max_concurrent_runs:
                try:
                    async with self._sessionmaker() as session:
                        got = await RunQueue(session).claim_next(
                            owner=self._instance, lease_s=self._settings.run_lease_s
                        )
                        await session.commit()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("领取 run 失败", exc_info=True)
                    break
                if got is None:
                    break
                run_id, takeover = got
                if takeover is not None:
                    logger.warning("接管 run %s（%s）", run_id, takeover)
                self._spawn(run_id, takeover)
                claimed += 1
        return claimed

    def _spawn(self, run_id: UUID, takeover: str | None) -> None:
        # ★ 本实例上还有这个 run 的旧任务（例如事件循环卡过、租约过期后自己又领了回来）：
        #   旧的必须停，同一个 run 不能有两个本地执行。
        #   已在收尾的（settled）不算：挂起后续跑时，上一段的任务正是在收尾里唤醒了这一段
        old = self._slots.get(run_id)
        if old is not None and not old.settled and old.task and not old.task.done():
            old.lost = True
            old.task.cancel()

        slot = ExecutionSlot()
        # ★ 必须给一个**全新的** contextvars.Context。
        #
        #   委派让 submit 第一次可能在「另一个 run 的执行上下文里」被调用
        #   （父 run 的 task 工具起子 run）。asyncio.create_task 默认拷贝当前
        #   上下文，而 langgraph 的流式输出与 langsmith 的追踪都挂在 contextvar
        #   上 —— 继承过去的话子 run 的 token 会被写进**父 run 的**流通道，
        #   子 run 自己的 message.delta 一个都不产出，最终落库的 assistant
        #   消息是空的。症状极具迷惑性：子 run 状态 succeeded、模型确实被调用、
        #   只是内容凭空消失。
        slot.task = asyncio.create_task(
            self._guarded(run_id, slot, takeover),
            name=f"run:{run_id}",
            context=contextvars.Context(),
        )
        self._slots[run_id] = slot

        # ★ 只删自己。续跑时上一段的任务还没退出、新一段已经领到了同一个 run_id：
        #   回调若无条件 pop，删掉的就是新任务 —— 那是它唯一的强引用（asyncio 对任务
        #   只持弱引用），新任务会在运行中被回收（2026-10-13 智能体评测：多次审批的
        #   run 偶发卡死）。
        def _forget(_t: asyncio.Task[None]) -> None:
            if self._slots.get(run_id) is slot:
                self._slots.pop(run_id, None)

        slot.task.add_done_callback(_forget)
        if self._renewer is None or self._renewer.done():
            self._renewer = asyncio.create_task(self._renew_forever(), name="run-lease-renewer")

    # ------------------------------------------------------------------ 续租

    async def _renew_forever(self) -> None:
        """每 lease/3 续一次本实例的全部 run；没有 run 了就退出（下次领取时再起）。"""
        interval = max(self._settings.run_lease_s / 3, 0.2)
        while self._slots:
            await asyncio.sleep(interval)
            try:
                await self.renew_leases()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 数据库抖动：这一轮没续上，下一轮再来。连续失败到租约过期 = 被接管，
                # 那时下一次续租会发现并停掉本地执行
                logger.warning("run 租约续期失败", exc_info=True)

    async def renew_leases(self) -> None:
        live = {rid: s for rid, s in self._slots.items() if not s.settled and not s.lost}
        if not live:
            return
        async with self._sessionmaker() as session:
            held = await RunQueue(session).renew_leases(
                list(live), owner=self._instance, lease_s=self._settings.run_lease_s
            )
            await session.commit()
        redis = self._redis()
        try:
            relay = EventRelay(redis)
            for run_id, slot in live.items():
                if slot.settled:  # 续租期间刚好收尾了
                    continue
                if run_id not in held:
                    # ★ 被接管了：立即停止本地执行，防止「假死后复活」的双写（§4.3）
                    logger.warning("run %s 的租约已被别的实例接管，停止本地执行", run_id)
                    slot.lost = True
                    if slot.task is not None:
                        slot.task.cancel()
                elif slot.thread_id is not None:
                    # 会话锁跟随租约（§4.4）
                    with contextlib.suppress(Exception):
                        await relay.renew_thread_lock(
                            slot.thread_id, owner=run_id, ttl_s=self._settings.run_lease_s
                        )
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()

    # ------------------------------------------------------------------ 交接

    async def drain(self) -> None:
        """停止领取，把在跑的 run 交给别的实例（§6）。可重复调用，只做一次。

        native 在下一个安全点（工具轮次落库之后）停下；acp 直接断开 —— 那一轮在 Pod 里
        照常进行，接手的实例 attach 回去。等不到安全点的（一个工具跑了很久）到
        drain_timeout_s 后直接中止，同样交接，最多丢这一轮。
        """
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain(), name="run-drain")
        await asyncio.shield(self._drain_task)

    async def _drain(self) -> None:
        self._draining = True
        # 等正在进行的那次领取结束：它领到的 run 也要在下面的名单里
        async with self._claim_lock:
            slots = list(self._slots.values())
        if slots:
            logger.info("停机：交接 %d 个在跑的 run", len(slots))
        for slot in slots:
            slot.drain = True
            if slot.cli and slot.task is not None:
                slot.task.cancel()
        tasks = [s.task for s in slots if s.task is not None and not s.task.done()]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=self._settings.drain_timeout_s)
            for task in pending:
                task.cancel()
            if pending:
                logger.warning("%d 个 run 没等到安全点，中止后交接", len(pending))
                await asyncio.wait(pending, timeout=10)
        if self._renewer is not None:
            self._renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._renewer

    async def _hand_off(self, run_id: UUID) -> None:
        """清掉租约（状态不动），唤醒别的实例立刻领走。"""
        try:
            async with self._sessionmaker() as session:
                released = await RunQueue(session).release_lease(run_id, owner=self._instance)
                await session.commit()
        except Exception:
            # 清不掉也不丢 run：租约到期后照样被接管，只是慢一个周期
            logger.warning("run %s 交接失败，等租约过期接管", run_id, exc_info=True)
            return
        if released:
            logger.info("run %s 已交接", run_id)
            await self._wake()

    # ------------------------------------------------------------------ 执行

    def _redis(self) -> aioredis.Redis:
        return make_redis(self._settings)

    async def _guarded(
        self, run_id: UUID, slot: ExecutionSlot, takeover: str | None = None
    ) -> None:
        try:
            if await self._segment.run(run_id, slot, takeover):
                # 停机中走到了安全点：交给别的实例
                await self._hand_off(run_id)
        except asyncio.CancelledError:
            # 停机交接、或被别的实例接管。前者清租约让别人立刻领走；后者什么都不能碰。
            # ★ 不再往上抛：取消是本执行器自己发起的，任务就此正常结束。
            if not slot.lost and not slot.settled:
                await self._hand_off(run_id)
        except LeaseLost:
            slot.lost = True
            logger.warning("run %s 的租约已被别的实例接管，本实例的这一段作废", run_id)
        except Exception:
            # 兜底：绝不让后台任务静默死掉，否则会话会永远停在 running
            logger.exception("run %s 执行时发生未捕获异常", run_id)
            if not slot.lost:
                with contextlib.suppress(Exception):
                    await self._segment.force_fail(run_id)
