"""Run 执行器 · 续跑、接管与取消（c4-model/agent/code-executor-continuation.html）。

一段结束之后的三件事：

    续跑    挂起的 run 等的事都有结果了，CAS 放回队列并唤醒。入口有三个（子 run 结束时、
            自己挂起落库后、定时补扫），判据只有一套（waits_are_settled）
    接管    领到交接或租约过期的 run，跑之前收拾上一个实例留下的现场（recover）
    取消    挂起中被取消的 run 没有进程，由这里落终态、放会话锁（settle_cancelled）

它不领取 run（调度与租约管），也不跑一段（一段的执行管）；放回队列之后由谁领走，
交给组合根注入的 requeue。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas_agent.domain.events import EventFactory, EventType, TraceEvent
from atlas_agent.domain.messages import interrupted_results

from ..config import Settings
from ..db.models import Run, Thread
from ..redisx import make_redis
from ..repositories.approval import ApprovalRepository
from ..repositories.continuation import ContinuationQueries
from ..repositories.event_archive import EventArchive
from ..repositories.run import SUSPENDED_STATUSES, RunRepository
from ..repositories.run_queue import RunQueue
from ..repositories.run_record import RunRecordRepository
from ..repositories.thread import ThreadRepository
from ..repositories.thread_context import ThreadContextRepository
from ..stream.relay import EventRelay

logger = logging.getLogger(__name__)

#: 不算「正文还在流」的事件 —— 接管时算要丢弃的正文用
_DELTAS = frozenset({EventType.MESSAGE_DELTA, EventType.THINKING_DELTA})

#: 放回队列之后：唤醒调度，让任意实例领走
Requeue = Callable[[UUID], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _discarded_chars(events: list[TraceEvent], depth: int) -> int:
    """接管时要丢弃的正文：最后一个非 delta 事件之后流出的那段（没落库，会重做）。"""
    chars = 0
    for event in events:
        if event.type not in _DELTAS:
            chars = 0
        elif event.type is EventType.MESSAGE_DELTA and event.depth == depth:
            chars += len(str(event.data.get("text") or ""))
    return chars


async def approval_decision(approvals: ApprovalRepository, token: str) -> str:
    """approval 行的状态；行丢了（或 token 非法）按 "failed"。"""
    try:
        row = await approvals.get(UUID(token))
    except ValueError:
        logger.warning("waiting_on 里的 approval token 非法：%r", token)
        return "failed"
    return "failed" if row is None else row.status


async def approval_outcome(approvals: ApprovalRepository, token: str) -> dict[str, Any]:
    """续跑交给图的审批结果（contracts/permission.py::ApprovalOutcome）。

    status 之外，计划审批带回用户选的执行模式，拒绝带回原因，问题带回答案（approval.outcome）。
    行丢了（或 token 非法）按 "failed"。
    """
    try:
        row = await approvals.get(UUID(token))
    except ValueError:
        logger.warning("waiting_on 里的 approval token 非法：%r", token)
        return {"status": "failed"}
    if row is None:
        return {"status": "failed"}
    extra = row.outcome if isinstance(row.outcome, dict) else {}
    picked = {k: extra[k] for k in ("mode", "feedback", "answers") if extra.get(k)}
    return {"status": row.status, **picked}


class Continuation:
    """一段结束之后怎么接着跑：续跑、接管时收拾现场、挂起中取消的收尾。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        instance: str,
        requeue: Requeue,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        #: 租约的 owner：接管时收拾现场的第一条写是租约围栏
        self._instance = instance
        self._requeue = requeue

    def _redis(self) -> aioredis.Redis:
        return make_redis(self._settings)

    # ------------------------------------------------------------------ 续跑

    async def resume_if_ready(self, run_id: UUID) -> bool:
        """等的东西都到了就把这个 suspended 的 run 续上。返回是否真的续了。

        ★ 三个调用点，同一套判据：子 run 跑完时、父自己挂起落库后（关竞态
          窗口）、周期 reaper 补扫时。做成一个方法是因为这三处漏掉任何一个
          都会让 run 永久卡在 suspended，而那是最难发现的故障形态 ——
          没有报错、没有事件，只是一个永远转圈的界面。

        ★ CAS 在 RunQueue.requeue 里。几个子 run 同时结束时它们都会走到这，
          只有一个能抢到。
        """
        async with self._sessionmaker() as session:
            run = await RunRepository(session).get(run_id)
            if run is None or run.status not in SUSPENDED_STATUSES:
                return False
            if not await self.waits_are_settled(session, run):
                return False
            if not await RunQueue(session).requeue(run_id):
                return False  # 别人抢到了
            await session.commit()

        # ★ 用户在等待期间点了取消 —— 别再装一次图、起一次任务去发现这件事。
        #   续跑段的 runner 循环当然也会检查取消位，但那要等到第一次 astream
        #   迭代，中间白装配一次图（MCP 解析、对象存储连接、模型构造）。
        if await self.cancel_requested(run_id):
            logger.info("run %s 在等待期间被取消，不再续跑", run_id)
            await self.settle_cancelled(run_id)
            return False

        logger.info("run %s 等的都有结果了，续跑", run_id)
        await self._requeue(run_id)
        return True

    async def waits_are_settled(self, session: AsyncSession, run: Run) -> bool:
        """这个 run 等的东西都有结果了吗 —— 续跑的 barrier。

        ★ 按 reason 分派，不是一套判据打天下：

            subagent    子 run 进终态
            approval    approval 行被决策（approved / rejected / expired）

          用 SubAgent 那套去判审批的话，barrier 会**立刻满足**（审批挂起时没有子
          run），于是续跑 → 又挂起 → 再续跑，死循环。这是 run.waiting_on
          那一列存在的全部理由（迁移 0012）。

        ★ 等待项是 {tool_call_id: {reason, token}}（contracts/suspension.py）。没有等待项
          的挂起（旧格式的数组）一律不续跑，用户可以取消它。CLI 在线等权限不是挂起，
          它是 running + awaiting_user，根本走不到这里。
        """
        waits = run.waiting_on if isinstance(run.waiting_on, dict) else {}
        if not waits:
            return False

        queries = ContinuationQueries(session)
        approvals = ApprovalRepository(session)
        for wait in waits.values():
            reason = (wait or {}).get("reason")
            token = (wait or {}).get("token") or ""
            if reason == "approval":
                # ★ 记录丢了算「已结清」，不是「继续等」：等一个不存在的审批 = run 永久
                #   挂起、会话一直被锁。续跑时按门禁故障告诉模型（SegmentLoader）。
                if await approval_decision(approvals, token) == "pending":
                    return False
            elif reason == "subagent":
                if await queries.has_unfinished_children(run.run_uuid):
                    return False
            else:
                # 不认识的 reason —— 不敢续跑（可能永远等不到），也不敢判死。
                # 记一条日志让它被发现，而不是静默卡住。
                logger.warning("run %s 的 waiting_on 里有未知 reason：%r", run.run_uuid, reason)
                return False
        return True

    async def sweep_suspended(self) -> int:
        """扫一遍挂起的 run，能续的续上。返回续了几个。

        ★ 为什么不能只靠子 run 主动唤醒：那个唤醒发生在子 run 收尾的代码里，
          而进程可能正好在那一刻被 SIGKILL。信号没了之后**没有任何其它机制**
          会再碰这个父 run。这个扫描是它唯一的出路。

        ★ 不判超时：审批只由人决定，等多久都不替用户按拒绝处理。
        """
        async with self._sessionmaker() as session:
            candidates = await ContinuationQueries(session).suspended_runs()
        resumed = 0
        for run_id in candidates:
            if await self.resume_if_ready(run_id):
                resumed += 1
        if resumed:
            logger.info("补唤醒了 %d 个等待中的 run", resumed)
        return resumed

    # ------------------------------------------------------------------ 取消

    async def cancel_requested(self, run_id: UUID) -> bool:
        redis = self._redis()
        try:
            return await EventRelay(redis).is_cancelled(run_id)
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()

    async def settle_cancelled(self, run_id: UUID) -> None:
        """把一个在等待中被取消的 run 收干净：落终态、发事件、放锁。

        ★ 这条收尾路径是 suspend 独有的。别处的取消都发生在**有进程在跑**
          的时候（runner 的循环自己发 run.cancelled 然后走正常收尾），而这里
          没有任何进程 —— 不自己收的话 run 会一直躺在 suspended，会话锁也
          一直不放。
        """
        redis = self._redis()
        relay = EventRelay(redis, ttl_s=self._settings.run_events_ttl_s)
        try:
            async with self._sessionmaker() as session:
                run = await RunRepository(session).get(run_id)
                if run is None:
                    return
                thread = await ThreadRepository(session).by_id(run.thread_uuid)
                if thread is None:
                    return
                stream_thread_id = thread.stream_thread_id
                archive = EventArchive(session)

                event = EventFactory(
                    run_id,
                    _utcnow,
                    start_seq=run.last_seq,
                    base_depth=run.depth,
                ).make(EventType.RUN_CANCELLED, {"partial_text_len": 0})
                # 序号在落库前盖好，与一段收尾同一条纪律
                event = event.stamped(
                    await relay.next_thread_seq(
                        stream_thread_id,
                        floor=await archive.thread_seq_floor(stream_thread_id),
                    )
                )
                await RunRecordRepository(session).finish(
                    run_id,
                    status="cancelled",
                    last_seq=event.seq,
                    usage={},  # 累加语义下空字典 = 不动账
                )
                await archive.archive_events([event], thread_id=stream_thread_id)
                await session.commit()
                thread_id = run.thread_uuid

            await relay.emit(event, thread_id=stream_thread_id)
            with contextlib.suppress(Exception):
                await relay.clear_cancel(run_id)
                await relay.release_thread_lock(thread_id, owner=run_id)
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()

    # ------------------------------------------------------------------ 接管

    async def recover(
        self,
        session: AsyncSession,
        run: Run,
        thread: Thread,
        relay: EventRelay,
        takeover: str,
    ) -> TraceEvent:
        """接管一个中途换手的 run（横向扩展设计 §5）：把上一个实例留下的现场收拾好。

            补归档    检查点之后发布到 Redis、还没归档的事件（流里有、库里没有的序号）
            续序号    run 内 seq 从上一个实例实际发到的地方接着数，不重号
            补结果    检查点里有 tool_use、没有结果的调用 → 「结果未知」（不盲目重放副作用）
            丢正文    最后一次模型调用已流出、没落库的正文字数 → 前端据此丢掉（会重做）

        返回 run.resumed。与检查点同一个事务里做，第一条写是围栏：租约若已不在本实例
        就整个回滚。
        """
        archive = EventArchive(session)
        stream_thread_id = thread.stream_thread_id
        lost = await relay.run_events_after(stream_thread_id, run.run_uuid, after_seq=run.last_seq)
        top = max([run.last_seq, *(e.seq for e in lost)])
        await RunRecordRepository(session).checkpoint(
            run.run_uuid, owner=self._instance, last_seq=top
        )
        archived = await archive.archived_thread_seqs(run.run_uuid, [e.thread_seq for e in lost])
        await archive.archive_events(
            [e for e in lost if e.thread_seq not in archived], thread_id=stream_thread_id
        )
        # acp 的 message 表里没有 tool_use（工具在 Pod 里），这一步对它天然是空操作
        context = await ThreadContextRepository(session).get(thread.thread_uuid)
        rows = await RunRepository(session).history(
            thread.thread_uuid, after=context.summary_upto if context is not None else None
        )
        if (sealing := interrupted_results(rows)) is not None:
            await ThreadRepository(session).add_messages(
                thread_id=thread.thread_uuid, messages=[sealing], run_id=run.run_uuid
            )
            logger.warning("run %s 接管：%d 个工具调用结果未知", run.run_uuid, len(sealing.content))
        base_depth = run.depth
        return EventFactory(run.run_uuid, _utcnow, start_seq=top, base_depth=base_depth).make(
            EventType.RUN_RESUMED,
            {
                "reason": takeover,
                "discard_chars": _discarded_chars(lost, base_depth),
                "takeovers": run.takeovers,
            },
        )
