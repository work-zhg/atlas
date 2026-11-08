"""Run 执行器 · 派发与租约（c4-model/agent/executor/code-executor-scheduling.html）。

这个 run 由哪个实例来跑（design-run-dispatch.html）：

    派发    run 跟着请求走：接到请求的实例占一个槽、按 run 号抢锁（CAS 写租约），抢到就在
            本实例跑。子 run 在父 run 所在的实例上起，续跑在触发它的实例上（审批决定的请求、
            最后结束的子 run）。占不到槽、抢不到锁都不报错，run 留在库里
    兜底    满了没接的、发版交出来的、崩溃后租约过期的、续跑信号丢了的，由每个实例都跑的
            兜底任务（RecoveryJob）分页拉出来、逐个抢锁，抢到就在本实例跑
    续租    LeaseRenewer 每 lease/3 一条 SQL 续本实例全部的 run；续不上 = 被别的实例抢走，立即停
    交接    停机时关准入，native 走到安全点、CLI 直接断开，清掉租约，交给别的实例的兜底任务

兜底和续租是定时任务库（atlas_jobs）的实例任务：这里只写 execute，循环、抖动、出错隔离、
停机由库负责（compose.register_jobs 注册）。一段怎么跑、怎么落库归「一段的执行」（segment.py）；
挂起的 run 什么时候该续、接管时怎么收拾现场归「续跑、接管与取消」（continuation.py）。
运行时由组合根装配好注入（compose.py），这里不认识任何一种运行时。

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
from atlas_jobs import Job, JobContext, NoParams
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..redisx import make_redis
from ..repositories.run import SUSPENDED_STATUSES, LeaseLost, RunRepository
from ..repositories.run_queue import RunQueue
from ..stream.relay import EventRelay
from .continuation import Continuation
from .segment import SegmentExecution, SegmentOutcome

logger = logging.getLogger(__name__)

#: 兜底任务每页拉几条候选：页小，抢锁冲突时白看的也少
RECOVERY_PAGE = 10


@dataclass
class ExecutionSlot:
    """本实例上一个在跑的 run（实现 segment.SegmentControl）。"""

    task: asyncio.Task[None] | None = None
    thread_id: UUID | None = None
    #: 收尾已开始落库：不再续租、续不上也不算「被抢走」（终态/挂起会清掉租约）
    settled: bool = False
    #: 租约已不在本实例（被别的实例抢走）：什么都不能再写，锁也不能放
    lost: bool = False
    #: 停机中：native 到安全点就交接
    drain: bool = False
    #: CLI 智能体：停机时直接断开（那一轮在 Pod 里照常进行）
    cli: bool = False

    def stop_lost(self) -> None:
        """租约已在别人手里：立即停掉本地执行，防止「假死后复活」的双写。"""
        self.lost = True
        if self.task is not None:
            self.task.cancel()


class SlotAdmission:
    """本实例还能不能接：并发上限减去在跑的槽。

    ★ 先占槽再抢锁：派发和兜底同时到也不会一起冲过上限；抢锁没成功就把槽还回去。
      挂起的 run 不占槽。停机中一律不给槽。
    """

    def __init__(self, max_concurrent: int) -> None:
        self.max_concurrent = max_concurrent
        self._used = 0
        self._closed = False

    @property
    def free(self) -> int:
        return 0 if self._closed else max(self.max_concurrent - self._used, 0)

    def reserve(self) -> bool:
        if self.free <= 0:
            return False
        self._used += 1
        return True

    def release(self) -> None:
        self._used = max(self._used - 1, 0)

    def close(self) -> None:
        self._closed = True


class RunWorker:
    """一个实例上的执行器，实现 RunExecutor。

    派发和兜底都落到同一个动作 try_take：先向准入占一个槽，再按 run 号抢锁，抢到就起执行槽
    交给「一段的执行」，抢不到就把槽还回去。一段结束后按结局续跑：自己挂起了再查一次条件，
    子 run 结束了去续父 run，放回排队的就地 try_take。
    api 角色的实例并发上限为 0、不跑兜底：它接到的 run 都留给别的实例的兜底任务。
    """

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
        #: api 角色只接请求、不执行（横向扩展设计 §3）
        self.executes = settings.server_role != "api"
        self.admission = SlotAdmission(settings.max_concurrent_runs if self.executes else 0)
        self._slots: dict[UUID, ExecutionSlot] = {}
        self._draining = False
        self._drain_task: asyncio.Task[None] | None = None
        #: 定时任务库的两个实例任务（compose.register_jobs 按周期注册）
        self.recovery = RecoveryJob(self, sessionmaker, settings, continuation)
        self.renewer = LeaseRenewer(self, sessionmaker, settings, instance=instance)

    # ------------------------------------------------------------------ 协议

    @property
    def model_builder(self) -> Any:
        """与执行器同一个模型构造器（手动压缩等 API 侧的模型调用用它，测试可注入）。"""
        return self._build_model

    @property
    def draining(self) -> bool:
        return self._draining

    async def dispatch(self, run_id: UUID) -> None:
        """派发：本实例占到槽、抢到锁就当场开跑；接不下也不报错，run 留在库里排队，
        排队超过几秒由兜底任务处理。调用方不等这一轮跑完。"""
        await self.try_take(run_id)

    async def try_take(self, run_id: UUID) -> bool:
        """占槽 → 按 run 号抢锁 → 起执行槽。抢到返回 True；没槽、没抢到返回 False。"""
        if not self.admission.reserve():
            return False
        try:
            async with self._sessionmaker() as session:
                taken = await RunQueue(session).take(
                    run_id, owner=self._instance, lease_s=self._settings.run_lease_s
                )
                await session.commit()
        except asyncio.CancelledError:
            self.admission.release()
            raise
        except Exception:
            # 抢不到不丢 run：它还在库里，兜底任务下一轮再来
            self.admission.release()
            logger.warning("抢 run %s 的锁失败", run_id, exc_info=True)
            return False
        if taken is None:
            self.admission.release()
            return False
        if taken.takeover is not None:
            logger.warning("接管 run %s（%s）", run_id, taken.takeover)
        self._start(run_id, taken.takeover)
        return True

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
        """等的事都有结果了就放回排队，并在本实例抢锁。返回是否放回了排队 ——
        本实例没抢到（满了、停机中）的留在排队里，由兜底任务处理。"""
        if not await self._continuation.resume_if_ready(run_id):
            return False
        await self.try_take(run_id)
        return True

    async def decided(self, run_id: UUID, approval_id: UUID) -> None:
        """审批组件的 DecisionListener：做了决定就查一次续跑条件，在接到决定的实例上续。"""
        await self.resume_if_ready(run_id)

    async def shutdown(self) -> None:
        """停机：交接在跑的 run。定时任务库由宿主在这之后停 —— 交接期间还要续租。"""
        await self.drain()

    # ------------------------------------------------------------------ 给两个实例任务

    def has_free_slot(self) -> bool:
        """还能不能再接一个：停机中、满了都不能。"""
        return self.admission.free > 0

    def live_slots(self) -> dict[UUID, ExecutionSlot]:
        """在跑、要续租的 run：已在收尾的和已被抢走的不算。"""
        return {rid: s for rid, s in self._slots.items() if not s.settled and not s.lost}

    # ------------------------------------------------------------------ 执行

    def _start(self, run_id: UUID, takeover: str | None) -> None:
        # ★ 本实例上还有这个 run 的旧任务（例如事件循环卡过、租约过期后自己又抢了回来）：
        #   旧的必须停，同一个 run 不能有两个本地执行。
        #   已在收尾的（settled）不算：挂起后续跑时，上一段的任务正是在收尾之后续上了这一段
        old = self._slots.get(run_id)
        if old is not None and not old.settled and old.task and not old.task.done():
            old.stop_lost()

        # 停机中抢到的（关准入之前已经在抢）：领到就交回去
        slot = ExecutionSlot(drain=self._draining)
        # ★ 必须给一个**全新的** contextvars.Context。
        #
        #   派发第一次可能在「另一个 run 的执行上下文里」被调用（父 run 的 task 工具起
        #   子 run）。asyncio.create_task 默认拷贝当前上下文，而 langgraph 的流式输出与
        #   langsmith 的追踪都挂在 contextvar 上 —— 继承过去的话子 run 的 token 会被写进
        #   **父 run 的**流通道，子 run 自己的 message.delta 一个都不产出，最终落库的
        #   assistant 消息是空的。症状极具迷惑性：子 run 状态 succeeded、模型确实被调用、
        #   只是内容凭空消失。
        slot.task = asyncio.create_task(
            self._guarded(run_id, slot, takeover),
            name=f"run:{run_id}",
            context=contextvars.Context(),
        )
        self._slots[run_id] = slot

        # ★ 只删自己。续跑时上一段的任务还没退出、新一段已经抢到了同一个 run_id：
        #   回调若无条件 pop，删掉的就是新任务 —— 那是它唯一的强引用（asyncio 对任务
        #   只持弱引用），新任务会在运行中被回收（2026-10-13 智能体评测：多次审批的
        #   run 偶发卡死）。
        def _forget(_t: asyncio.Task[None]) -> None:
            if self._slots.get(run_id) is slot:
                self._slots.pop(run_id, None)

        slot.task.add_done_callback(_forget)

    async def _guarded(
        self, run_id: UUID, slot: ExecutionSlot, takeover: str | None = None
    ) -> None:
        outcome: SegmentOutcome | None = None
        try:
            outcome = await self._segment.run(run_id, slot, takeover)
        except asyncio.CancelledError:
            # 停机交接、或被别的实例抢走。前者清租约让别人的兜底任务抢走；后者什么都不能碰。
            # ★ 不再往上抛：取消是本执行器自己发起的，任务就此正常结束。
            if not slot.lost and not slot.settled:
                await self._hand_off(run_id)
        except LeaseLost:
            slot.lost = True
            logger.warning("run %s 的租约已被别的实例抢走，本实例的这一段作废", run_id)
        except Exception:
            # 兜底：绝不让后台任务静默死掉，否则会话会永远停在 running
            logger.exception("run %s 执行时发生未捕获异常", run_id)
            if not slot.lost:
                with contextlib.suppress(Exception):
                    await self._segment.force_fail(run_id)
        finally:
            # ★ 先还槽再续跑：子 run 结束后父 run 要在本实例抢锁，用的正是这个刚空出的槽
            self.admission.release()
        if outcome is not None:
            await self._follow(run_id, outcome)

    async def _follow(self, run_id: UUID, outcome: SegmentOutcome) -> None:
        """一段结束之后：交接、查自己的续跑条件、续父 run。"""
        try:
            if outcome.kind == "handed_off":
                await self._hand_off(run_id)
            elif outcome.kind == "suspended":
                # ★ 必须在落库之后**再查一次**续跑条件，否则有一个必然会踩到的竞态：
                #   子 run 可能在父这段收尾之前就跑完了，那一刻它去续父 run，而父还是
                #   running —— CAS 失败，续跑信号丢掉；随后父落成 suspended，从此再没有
                #   人来叫它。委派越短这个窗口命中率越高（子 run 秒回时几乎必现）。
                await self.resume_if_ready(run_id)
            elif outcome.parent_to_resume is not None:
                # 子 run 跑完了：去续等着它的父 run。父 run 在这里（最后一个结束的子 run
                # 所在的实例）抢锁 —— 它刚空出一个槽
                await self.resume_if_ready(outcome.parent_to_resume)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 续跑信号丢了不丢 run：兜底任务会补查到它
            logger.warning("run %s 一段结束后续跑失败，留给兜底任务", run_id, exc_info=True)

    async def _hand_off(self, run_id: UUID) -> None:
        """清掉租约（状态不动），别的实例的兜底任务下一轮就抢走。"""
        try:
            async with self._sessionmaker() as session:
                released = await RunQueue(session).release(run_id, owner=self._instance)
                await session.commit()
        except Exception:
            # 清不掉也不丢 run：租约到期后照样被接管，只是慢一个租约周期
            logger.warning("run %s 交接失败，等租约过期接管", run_id, exc_info=True)
            return
        if released:
            logger.info("run %s 已交接", run_id)

    def _redis(self) -> aioredis.Redis:
        return make_redis(self._settings)

    # ------------------------------------------------------------------ 交接

    async def drain(self) -> None:
        """停机交接：关准入，把在跑的 run 交给别的实例。可重复调用，只做一次。

        native 在下一个安全点（工具轮次落库之后）停下；CLI 直接断开 —— 那一轮在 Pod 里
        照常进行，接手的实例再连回去。等不到安全点的（一个工具跑了很久）到 drain_timeout_s
        后直接中止，同样交接，最多重做这一轮。兜底任务看到停机中就不再抢。
        """
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain(), name="run-drain")
        await asyncio.shield(self._drain_task)

    async def _drain(self) -> None:
        self._draining = True
        self.admission.close()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.drain_timeout_s
        announced = False
        # ★ 循环着等：关准入之前已经在抢锁的那几次，抢到后也会起槽（带着 drain 标记）
        while running := [s for s in self._slots.values() if s.task and not s.task.done()]:
            if not announced:
                logger.info("停机：交接 %d 个在跑的 run", len(running))
                announced = True
            for slot in running:
                if not slot.drain:
                    slot.drain = True
                    if slot.cli and slot.task is not None:
                        slot.task.cancel()
            tasks = [s.task for s in running if s.task is not None]
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning("%d 个 run 没等到安全点，中止后交接", len(tasks))
                for task in tasks:
                    task.cancel()
                await asyncio.wait(tasks, timeout=10)
                return
            await asyncio.wait(tasks, timeout=remaining)


class RecoveryJob(Job):
    """兜底任务：定时任务库的实例任务，每个实例一份、逻辑一样，每 10 秒左右一轮。

    满了没接的、发版交出来的、崩溃后租约过期的、续跑信号丢了的，都是「该接手的那一刻没人接」，
    run 留在了库里。这里按 id 往后翻页拉候选（每页 10 条），逐个抢锁，抢到就在本实例跑，
    抢不到看下一个；翻完或者没空槽就停，剩下的留给别的实例 —— 闲的多抢、忙的少抢。

    ★ 不用选主：抢锁本身保证一个 run 只被一个实例拿到。也不能做成全集群一份的集群任务：
      一个实例崩溃留下的 run 会全压到一个实例上。
    """

    type = "agent.run_recovery"

    def __init__(
        self,
        worker: RunWorker,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        continuation: Continuation,
    ) -> None:
        self._worker = worker
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._continuation = continuation

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        # 停机中、满了：直接返回，留给别的实例
        if not self._worker.has_free_slot():
            return
        taken = await self._take_orphans() + await self._resume_suspended()
        if taken:
            logger.info("兜底：本实例接下 %d 个 run", taken)

    async def _take_orphans(self) -> int:
        """排队超过几秒没人接的、租约过期的、交接了没人接的：逐个抢锁。"""
        taken = 0
        after = 0
        while self._worker.has_free_slot():
            async with self._sessionmaker() as session:
                page = await RunQueue(session).candidates(
                    after_id=after,
                    limit=RECOVERY_PAGE,
                    queued_for_s=self._settings.run_recovery_queued_s,
                )
            if not page:
                break
            for candidate in page:
                after = candidate.id
                if not self._worker.has_free_slot():
                    break
                if await self._worker.try_take(candidate.run_uuid):
                    taken += 1
        return taken

    async def _resume_suspended(self) -> int:
        """续跑信号丢了的挂起 run：SQL 先滤过一遍，剩下的逐个确认续跑条件，成立就放回排队、抢锁。"""
        resumed = 0
        after = 0
        while self._worker.has_free_slot():
            page = await self._continuation.suspended_candidates(
                after_id=after, limit=RECOVERY_PAGE
            )
            if not page:
                break
            for candidate in page:
                after = candidate.id
                if not self._worker.has_free_slot():
                    break
                if await self._worker.resume_if_ready(candidate.run_uuid):
                    resumed += 1
        return resumed


class LeaseRenewer(Job):
    """续租：定时任务库的实例任务，每个实例一份，每 lease/3 一轮。

    一条 SQL 续本实例全部在跑的 run；某个 run 续不上，说明它已被别的实例抢走（本实例卡顿过久），
    立即停掉它的执行槽。会话锁跟着租约一起续。

    ★ 不放进每个 run 自己的流程里续：run 大部分时间在等模型和工具，一个工具可能跑几十分钟，
      那段时间续不上。停机交接期间它要一直跑，所以宿主交接完才停定时任务库。
    """

    type = "agent.run_lease_renewer"

    def __init__(
        self,
        worker: RunWorker,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        instance: str,
    ) -> None:
        self._worker = worker
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._instance = instance

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        live = self._worker.live_slots()
        if not live:
            return
        async with self._sessionmaker() as session:
            held = await RunQueue(session).renew(
                list(live), owner=self._instance, lease_s=self._settings.run_lease_s
            )
            await session.commit()
        redis = make_redis(self._settings)
        try:
            relay = EventRelay(redis)
            for run_id, slot in live.items():
                if slot.settled:  # 续租期间刚好收尾了
                    continue
                if run_id not in held:
                    logger.warning("run %s 的租约已被别的实例抢走，停止本地执行", run_id)
                    slot.stop_lost()
                elif slot.thread_id is not None:
                    # 会话锁跟随租约（§4.4）
                    with contextlib.suppress(Exception):
                        await relay.renew_thread_lock(
                            slot.thread_id, owner=run_id, ttl_s=self._settings.run_lease_s
                        )
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()
