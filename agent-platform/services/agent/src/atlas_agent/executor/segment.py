"""Run 执行器 · 一段的执行与落库（c4-model/agent/code-executor-segment.html）。

领到之后怎么跑、跑完留下什么：从库里还原这一段（SegmentLoader），按智能体类型选运行时
（RuntimeRegistry），把运行时产出的事件逐个盖序号、发布、攒着归档；每个工具轮次落一次检查点
（消息、事件、序号、变了的审批模式和待办，一个事务，第一条写校验租约）；段结束时按结果收尾：
终态、挂起或交接。收尾事件先落库再发布，前端看到它时库里已经是对应状态。

它不管 run 由哪个实例领（调度与租约管），也不管挂起后何时续、接管时怎么收拾现场（续跑、
接管与取消管），更不管一段在运行时内部怎么跑（AgentRuntime 的实现管）。

★ 前后两段——载入/标记运行中，与收事件/落库/释放锁——与智能体类型无关，接一种新的运行时
  只实现 AgentRuntime（acp 详设 §02）。

后台任务**必须自建 session 与 Redis 连接** —— 创建它的那个 HTTP 请求早已结束，
其 session 也已关闭。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any, Protocol
from uuid import UUID

import redis.asyncio as aioredis
from atlas_engine.kernel.middleware.compaction import SUMMARY_PREFIX
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas_agent.domain.events import EventType, TraceEvent
from atlas_agent.domain.messages import (
    KIND_CHAT,
    StoredMessage,
    Transcript,
    from_storage,
)
from atlas_agent.domain.spec import AgentSpec, spec_for_subagent

from ..approval import ApprovalService
from ..config import Settings
from ..db.models import AgentVersion, Message, Run, Thread
from ..outbound import CallIdentity, use_identity
from ..redisx import make_redis
from ..repositories.cli_session import CliSessionRepository
from ..repositories.event_archive import EventArchive
from ..repositories.run import LeaseLost, RunRepository
from ..repositories.run_record import RunRecordRepository
from ..repositories.skill_usage import SkillUsageRepository
from ..repositories.thread import ThreadRepository
from ..repositories.thread_context import ThreadContextRepository
from ..schemas.agent import AgentSpecIn
from ..services.subagent import subagent_result
from ..stream.relay import EventRelay
from ..telemetry.run_trace import RunTrace
from .assembly import PreparedRun
from .continuation import Continuation
from .runtime import AgentRuntime, NativeRuntime, select_runtime

logger = logging.getLogger(__name__)

_STATUS_BY_TERMINAL = {
    EventType.RUN_FINISHED: "succeeded",
    EventType.RUN_FAILED: "failed",
    EventType.RUN_CANCELLED: "cancelled",
}


def _last_chat_input(rows: list[Message]) -> int | None:
    """最后一条 kind='chat' 的用户消息在 rows 里的下标。None = 一条都没有。"""
    for index in range(len(rows) - 1, -1, -1):
        if rows[index].role == "user" and rows[index].kind == KIND_CHAT:
            return index
    return None


async def _save_approval_mode(
    session: AsyncSession, run_id: UUID, thread_id: UUID, mode: str
) -> None:
    """计划获批后的新模式。

    run 续跑、接管时从 run.approval_mode 读；会话记住它，作用于下一条消息。
    """
    await session.execute(update(Run).where(Run.run_uuid == run_id).values(approval_mode=mode))
    await session.execute(
        update(Thread).where(Thread.thread_uuid == thread_id).values(approval_mode=mode)
    )


def _spec_from_version(version: AgentVersion, *, slug: str, name: str) -> AgentSpec:
    return AgentSpecIn.model_validate(version.spec).to_engine(slug=slug, name=name)


def _today() -> date:
    return datetime.now(UTC).date()


class SegmentControl(Protocol):
    """调度与一段的执行之间的信号：调度的执行槽（ExecutionSlot）实现它。

    drain    停机中：native 到安全点就交接
    settled  收尾已开始落库：不再续租、续不上也不算「被接管」
    lost     租约已不在本实例：什么都不能再写，锁也不能放
    """

    thread_id: UUID | None
    settled: bool
    lost: bool
    drain: bool
    cli: bool


class RuntimeRegistry:
    """按智能体类型选运行时。运行时由组合根注册（native 必有，cli 部署接了才有）。

    ★ 选择只在这里发生一次：散在执行器里的话，加一种智能体类型要找齐所有分支点。
    """

    def __init__(self, *, native: NativeRuntime, cli: AgentRuntime | None = None) -> None:
        self._native = native
        self._cli = cli

    def select(self, prepared: PreparedRun) -> AgentRuntime:
        return select_runtime(prepared, native=self._native, cli=self._cli)


class SegmentLoader:
    """从库里还原这一段：spec、本轮输入、历史（摘要边界之前用摘要代替）、挂起现场、
    审批模式和跨段 state。"""

    async def load(
        self,
        session: AsyncSession,
        run: Run,
        thread: Thread,
        version: AgentVersion,
    ) -> PreparedRun:
        """从库里还原「任务是什么」（spec / 输入 / 历史 / 摘要边界）。"""
        from ..repositories.agent import AgentRepository

        found = await AgentRepository(session).get(thread.agent_uuid)
        # agent 行理论上必然存在（thread.agent_id 有外键），兜底只为不炸
        slug, name = (found[0].slug, found[0].name) if found is not None else ("agent", "agent")

        spec = _spec_from_version(version, slug=slug, name=name)

        # ★ 子会话的 run：从父 agent 的同一份快照里派生出子智能体自己的
        #   AgentSpec。子会话因此走与普通 run **完全同一条**链路 ——
        #   历史重建、压缩、步数刹车、审批、取消、孤儿回收全部原样复用。
        if thread.subagent_name:
            spec = spec_for_subagent(spec, thread.subagent_name)

        # 审批模式（doc/native-approval-mode-design.html）：run 上记的是建 run 时
        # （或计划获批后）的模式，子 run 记的是派活那一刻父 run 的模式。
        # native 按智能体允许的范围校正；acp 原样交下去，由 HostRuntime 与它自己的
        # permission_mode 取更严格的
        approval_mode = (
            spec.approval.resolve(run.approval_mode or thread.approval_mode)
            if spec.kind == "native"
            else run.approval_mode
        )

        # CLI 智能体：会话的接续信息（cli_session）。会话级的，跨 run、跨实例都从库里读
        cli: dict[str, Any] = {}
        cli_sessions = CliSessionRepository(session)
        if spec.kind == "cli" and (row := await cli_sessions.get(thread.thread_uuid)):
            cli = {
                "cli_session_id": row.agent_session_id,
                "cli_bridge_instance": row.bridge_instance,
                "cli_acked_seq": row.acked_seq,
            }

        # 上下文状态（thread_context）：摘要替换掉覆盖边界之前的消息，熔断计数交给压缩
        context = await ThreadContextRepository(session).get(thread.thread_uuid)
        summary_upto = context.summary_upto if context is not None else None
        failures = context.failure_count if context is not None else 0
        compacted = context.compact_count if context is not None else 0

        rows = await RunRepository(session).history(thread.thread_uuid, after=summary_upto)
        if not rows:
            return PreparedRun(
                **cli,
                compact_count=compacted,
                compaction_failures=failures,
                spec=spec,
                input_content="",
                thread=thread,
                agent_version_id=version.agent_version_uuid,
                approval_mode=approval_mode,
            )

        # ★ 本轮输入是最后一条 **kind='chat' 的用户消息**，不是最后一条消息。
        #   工具结果在 Anthropic 的格式里同样是 role='user'（domain/messages.py），
        #   不加区分就会把一批工具结果当成用户的新提问 —— 表现是模型莫名回答
        #   一段 JSON，而用户真正的问题被归进了历史。
        input_index = _last_chat_input(rows)

        # ★ 输入消息之后还有东西 = 这个 run 之前已经跑过一段（挂起后续跑）。
        #   那时本轮输入早已在历史里，再 append 一遍会让模型看到同一个问题
        #   问了两次。判据取自数据本身，不靠额外的标记位。
        resume = input_index is not None and input_index < len(rows) - 1

        if input_index is None or resume:
            history = from_storage(rows)
            input_content: Any = ""
            upto = rows[-1].created_at
        else:
            history = from_storage(rows[:input_index])
            input_content = rows[input_index].content
            upto = rows[input_index - 1].created_at if input_index > 0 else None

        # ★ 续跑：消息原样交回、**不改写**，挂起现场（waiting_on + decisions）另交。
        #   被拦下的调用在消息里悬空，图内的中间件按 waiting_on 认出它在等什么、
        #   按 decisions 执行或填结论（contracts/suspension.py）。
        waiting_on, decisions = await self.resume_state(session, run, history)

        # §7.4：已压缩的早期消息以摘要形式回填，而不是重新送原文。
        # 没有这一步，每个 run 都要重压一次 —— 摘要 token 重复付、
        # prompt cache 每轮击穿（§7.3 坑 2 的抖动）。
        if context is not None and context.summary:
            history.insert(0, AIMessage(content=SUMMARY_PREFIX + context.summary))

        return PreparedRun(
            **cli,
            compact_count=compacted,
            compaction_failures=failures,
            spec=spec,
            input_content=input_content,
            history=history,
            history_upto=upto,
            thread=thread,
            agent_version_id=version.agent_version_uuid,
            resume=resume,
            # 本轮用户消息的时间：Agent 这一轮写入记忆时的「信息成立时间」（记忆设计 §6.4）
            input_at=rows[input_index].created_at if input_index is not None else None,
            start_seq=run.last_seq,
            prior_tokens=run.total_tokens,
            # ★ 子孙 run 的事件带它在 run 树里的深度（子 1、孙 2 ……），建子 run 时记下
            #   （迁移 0021）。SubAgent 可以继续派 SubAgent，不能再按「有没有父 run」判 0 / 1。
            base_depth=run.depth,
            waiting_on=waiting_on,
            decisions=decisions,
            approval_mode=approval_mode,
            # ★ 与 waiting_on 一样从库里读：续跑段、接管段可能落在任何一个实例上
            run_state=await RunRecordRepository(session).load_state(run.run_uuid),
        )

    async def resume_state(
        self, session: AsyncSession, run: Run, history: list[BaseMessage]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """续跑交回图的挂起现场：(还在等的调用, 已经有的结果)。

        ★ 两份依据必须一起交回（demo/suspend-resume 要点 1）：消息里只看得出某个调用
          还没结果，看不出它在等什么。漏了 waiting_on，图入口的 Patch 会把「在等」当成
          「被放弃」补一句 cancelled，模型照着回答，被批准的工具永远不执行。

        ★ 剪掉历史里已经有结果的项。run.waiting_on 要留到 run 结束（续跑段可能被任意
          实例领取，只能从库里读），这期间检查点会先把结果落进 message 表；接管时还会
          给悬空的调用补「结果未知」。这些都已经不在等了 —— 交给图的话，before_model
          会为它们跳 tools，而 ToolNode 执行的是**最后一条** AIMessage 的调用。

        ★ decisions 只放已经有结果的（contracts/suspension.py）：

            approval    ApprovalOutcome：approval 行的状态，加上计划审批选的模式、拒绝原因；
                        pending 不放；行丢了按 "failed"（门禁故障）——
                        关卡已判它结清，图里却拿不到决策的话，图跳出、关卡放行，死循环
            subagent    子 run 的结论文本（关卡保证子 run 都已终态）
        """
        waits = run.waiting_on if isinstance(run.waiting_on, dict) else {}
        answered = {m.tool_call_id for m in history if isinstance(m, ToolMessage)}
        waits = {cid: w for cid, w in waits.items() if cid not in answered and isinstance(w, dict)}
        decisions: dict[str, Any] = {}
        approvals = ApprovalService(session)
        for wait in waits.values():
            token = str(wait.get("token") or "")
            if wait.get("reason") == "approval":
                outcome = await approvals.result(token)
                if outcome["status"] != "pending":
                    decisions[token] = outcome
            elif wait.get("reason") == "subagent":
                try:
                    decisions[token] = await subagent_result(session, UUID(token))
                except ValueError:  # token 不是个合法 UUID —— 不该发生
                    logger.warning("waiting_on 里的 sub_run_id 非法：%r", token)
                    decisions[token] = "SubAgent 的子 run 记录无法解析，本轮拿不到它的结论。"
            else:
                logger.warning("run %s 的 waiting_on 里有未知 reason：%r", run.run_uuid, wait)
        return waits, decisions


class SegmentExecution:
    """跑一段并留下记录，只编排。出站身份按 run 行重建，不继承任何请求。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        instance: str,
        runtimes: RuntimeRegistry,
        continuation: Continuation,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        #: 租约的 owner：检查点和收尾的第一条写都按它校验
        self._instance = instance
        self._runtimes = runtimes
        self._continuation = continuation
        self._loader = SegmentLoader()

    def _redis(self) -> aioredis.Redis:
        return make_redis(self._settings)

    async def run(self, run_id: UUID, slot: SegmentControl, takeover: str | None = None) -> bool:
        """跑一段并留下记录。返回 True = 停机中走到了安全点，交给调度去交接。

        顺序：载入 → 接管来的先收拾现场 → 标记运行中 → 会话锁跟着租约续上 → 选运行时 →
        事件逐个盖序号、发布、攒着归档 → 每个工具轮次落一次检查点 → 安全点上响应交接 →
        段末按结果收尾；挂起收尾后再查一次续跑条件。
        """
        redis = self._redis()
        relay = EventRelay(redis, ttl_s=self._settings.run_events_ttl_s)
        #: 自上一个检查点以来发布的事件 —— 下一个检查点 / 收尾时归档
        collected: list[TraceEvent] = []
        #: 已分配出去的最后一个 run 内序号（检查点之后 collected 会清空，不能从它推）
        last_seq = 0
        # ★ 必须先初始化：load_for_execution 返回 None 时直接 return，
        #   而 return 要经过 finally —— 那里会读它。
        thread_id: UUID | None = None
        parent_run_id: UUID | None = None
        #: 这一段是挂起收尾的（还在等子 run），不是这一轮结束了。
        #: finally 里据此**不**释放会话锁、不清取消位 —— run 还没完。
        suspended = False
        #: 这一段被交接 / 取消了：run 没结束，锁与取消位都要留给接手的实例
        interrupted = False

        try:
            async with self._sessionmaker() as session:
                records = RunRecordRepository(session)
                loaded = await records.load_for_execution(run_id)
                if loaded is None:
                    # ★ run 行在、会话或配置版本没了（会话被删）。判失败收掉 —— 否则它会
                    #   一遍遍被领取、一遍遍跳过
                    logger.error("run %s 的会话或配置版本不存在，判失败", run_id)
                    await records.finish(
                        run_id,
                        status="failed",
                        last_seq=0,
                        usage={},
                        error_kind="orphaned",
                        error_message="会话或配置版本已不存在",
                        owner=self._instance,
                    )
                    await ApprovalService(session).expire_run(run_id)
                    await session.commit()
                    return False
                run, thread, version = loaded
                thread_id = thread.thread_uuid
                slot.thread_id = thread_id
                # ★ 出站身份（内网身份传递 §7.2）：这一轮发出的一切内网调用（知识库、技能应用、
                #   用户中心）都带它。执行任务是全新的上下文、不继承任何请求；接管的实例同样
                #   按 run 行重建。auth_uid 与发起人不同只会出现在这里：以 Agent 为权限时
                #   是数字员工（建 run 时按 Agent 的权限类型取好，数字员工设计 §7.3）。
                initiator = run.created_by_uuid or thread.created_by_uuid
                use_identity(CallIdentity(uid=initiator, auth_uid=run.auth_uid or initiator))
                parent_run_id = run.parent_run_uuid
                # ★ 事件流归属**根会话**：子 run 的事件也进父会话那条流，
                #   否则「一个会话看到所有事」不成立（见 Thread.stream_thread_id）。
                stream_thread_id = thread.stream_thread_id
                # Redis 的序号 key 丢失时的恢复水位。每段查一次，不是每事件。
                seq_floor = await EventArchive(session).thread_seq_floor(stream_thread_id)

                resumed: TraceEvent | None = None
                if takeover is not None:
                    resumed = await self._continuation.recover(
                        session, run, thread, relay, takeover
                    )
                    # recover 的 UPDATE 让 ORM 行过期了；异步会话不能懒加载，显式刷新
                    await session.refresh(run)
                prepared = await self._loader.load(session, run, thread, version)
                if resumed is not None:
                    prepared = replace(prepared, start_seq=resumed.seq, takeover=takeover)
                await records.mark_running(run_id)
                await session.commit()

            slot.cli = prepared.spec.kind == "cli"
            last_seq = prepared.start_seq
            # 会话锁跟随租约（§4.4）：接管时原来的锁多半已经过期，重新占上
            with contextlib.suppress(Exception):
                await relay.renew_thread_lock(
                    thread_id, owner=run_id, ttl_s=self._settings.run_lease_s
                )
            if resumed is not None:
                collected.append(
                    await relay.publish(resumed, thread_id=stream_thread_id, floor=seq_floor)
                )
            if slot.drain and slot.cli:
                raise asyncio.CancelledError  # 领到时已在停机：直接交回去

            assistant_content: list[dict] | None = None
            usage: dict[str, int] = {}
            #: 这一段的收尾事件 —— 终态（run 结束）或 run.suspended（还要续）。
            segment_end: TraceEvent | None = None
            title: dict[str, Any] | None = None
            # ★ 本轮实际产生的消息（模型的 tool_use、工具的结果）。落库它们
            #   是分段执行的前提 —— 段与段之间没有别的载体（§7.4 的「没有
            #   checkpointer」在这里仍然成立：事实源始终是 message 表）。
            transcript = Transcript()
            #: native 走到了安全点、且本实例在停机：交接
            hand_off = False
            #: 本段里审批模式变了（计划获批）、还没落库的新值
            mode_changed: str | None = None
            #: 本段里图 state 变了、还没落库的键（run_state 表）。与 mode_changed 同一条落库路径
            state_changed: dict[str, Any] = {}

            # ★ 「一轮怎么跑」在 runtime 里（acp 详设 §02）。本方法的前后两段
            #   ——载入/标记运行中，与收事件/落库/释放锁——与 agent 类型无关，
            #   acp 接进来时一行都不用改。
            runtime = self._runtimes.select(prepared)
            stream = runtime.run_turn(
                prepared, run_id=run_id, redis=redis, relay=relay, transcript=transcript
            )
            # ★ Run 是 trace 的根（可观测性设计 §04）。子 span 由事件流派生 ——
            #   native 与 acp 产出同形事件，所以这一处覆盖两条执行路径。
            #   未启用遥测时 RunTrace 里拿到的是 no-op span，零开销。
            with RunTrace.start(
                prepared,
                run_id=str(run_id),
                parent_run_id=str(parent_run_id) if parent_run_id else None,
            ) as run_trace:
                # ★ aclosing：交接时中途 break，生成器要被关掉（图停下、bridge 连接断开）
                async with contextlib.aclosing(stream):
                    async for event in stream:
                        run_trace.observe(event)

                        # ★ 收尾事件**先落库再发布**。否则客户端收到 run.finished 后
                        #   立刻 GET /runs/{id} 会读到 status=running、tokens=0 ——
                        #   而前端正是这么用的（收到终止事件就去取最终用量）。
                        #   这条顺序保证："看到收尾事件" ⇒ "DB 已是对应的状态"。
                        #   挂起同理：看到 run.suspended ⇒ DB 已是 suspended。
                        #
                        #   序号却要**现在**就盖上 —— 归档按 thread_seq 落库，而它是
                        #   主键的一半。分配与发布因此分成两步（relay.emit）。
                        if event.ends_segment:
                            segment_end = event.stamped(
                                await relay.next_thread_seq(stream_thread_id, floor=seq_floor)
                            )
                            collected.append(segment_end)
                            last_seq = segment_end.seq
                            break

                        # publish 的返回值必须用上 —— 会话级序号是它盖的
                        collected.append(
                            await relay.publish(event, thread_id=stream_thread_id, floor=seq_floor)
                        )
                        last_seq = event.seq
                        if event.type is EventType.MESSAGE_COMPLETED:
                            assistant_content = event.data.get("content")
                        elif event.type is EventType.USAGE_UPDATED:
                            usage = {k: v for k, v in event.data.items() if isinstance(v, int)}
                        elif event.type is EventType.TITLE_GENERATED:
                            title = dict(event.data)
                        elif (
                            event.type is EventType.AGENT_MODE
                            and event.data.get("source") == "plan_approved"
                        ):
                            # ★ 计划获批、模式变了：随下一次检查点（与那条工具结果同一个事务）
                            #   或段收尾落库。只在收尾时写的话，检查点之后进程死掉，接管者会按
                            #   旧的规划模式继续，而「计划已批准」的结果已经在消息里了
                            mode_changed = str(event.data.get("effective") or "") or None
                        elif event.type is EventType.TODOS_UPDATED:
                            # ★ 待办是图 state 里的字段，不在 message 表里：不落库的话续跑、
                            #   接管之后 state.todos 是空的（压缩中间件的待办提醒读的就是它）
                            state_changed["todos"] = event.data.get("todos") or []

                        # ★ 检查点（§5.1）：模型说完、或一批工具结果收齐，就落库。
                        #   进程在这之后死掉，接手的实例从这里接着跑。
                        if rows := transcript.checkpoint_rows():
                            await self._checkpoint(
                                run_id,
                                thread_id=thread_id,
                                stream_thread_id=stream_thread_id,
                                rows=rows,
                                events=collected,
                                last_seq=last_seq,
                                approval_mode=mode_changed,
                                run_state=state_changed,
                            )
                            collected = []
                            mode_changed = None
                            state_changed = {}
                        # ★ 安全点：本轮产生的都已落库、没有在跑的工具 —— 停在这里不丢任何工作
                        if (
                            slot.drain
                            and transcript.flushed == len(transcript.messages)
                            and not transcript.pending_calls()
                        ):
                            hand_off = True
                            break

            if hand_off:
                interrupted = True
                if collected:  # 安全点之后、交接之前发布的事件（通常没有）
                    with contextlib.suppress(Exception):
                        await self._checkpoint(
                            run_id,
                            thread_id=thread_id,
                            stream_thread_id=stream_thread_id,
                            rows=[],
                            events=collected,
                            last_seq=last_seq,
                            approval_mode=mode_changed,
                            run_state=state_changed,
                        )
                return True

            suspended = segment_end is not None and segment_end.type is EventType.RUN_SUSPENDED

            # 从这里起是收尾：终态 / 挂起都会清掉租约，续租不再管它
            slot.settled = True
            await self._persist(
                run_id=run_id,
                thread_id=thread_id,
                stream_thread_id=stream_thread_id,
                events=collected,
                last_seq=last_seq,
                assistant_content=assistant_content,
                usage=usage,
                segment_end=segment_end,
                title=title,
                transcript=transcript,
                suspended=suspended,
                approval_mode=mode_changed,
                run_state=state_changed,
            )
            if segment_end is not None:
                # 序号已在 break 处盖好 —— 这里只发布
                await relay.emit(segment_end, thread_id=stream_thread_id)

            if suspended:
                # ★ 必须在落库之后**再查一次** join barrier，否则有一个必然会
                #   踩到的竞态：子 run 可能在父这段收尾之前就跑完了，那一刻它
                #   去唤醒父 run，而父还是 running —— CAS 失败，唤醒丢掉；随后
                #   父落成 suspended，从此再没有人来叫它。
                #   委派越短这个窗口命中率越高（子 run 秒回时几乎必现）。
                await self._continuation.resume_if_ready(run_id)
                return False

            # ★ 记忆不在 run 收尾时抽取：Agent 在对话中自己保存，漏记的由会话空闲后的补记补上
            #   （doc/memory-design.html §4.3；维护者调度，memory/backfill.py）。

            # ★ 这是个**子 run**，而且刚跑完 —— 去叫醒等着它的父 run。
            #   放在最后：父续跑会起一个新任务，不该让它排在本 run 的收尾前面。
            if parent_run_id is not None:
                await self._continuation.resume_if_ready(parent_run_id)
            return False
        except asyncio.CancelledError:
            interrupted = True
            raise
        except LeaseLost:
            slot.lost = True
            raise
        finally:
            # ★ suspend 不释放会话锁、不清取消位 —— 这一轮没结束。
            #
            #   锁要一直拿着：放了的话用户能在等待期间发第二条消息，两个 run
            #   交错写同一个 thread，「同一会话串行」就破了。
            #   取消位要留着：用户在等待期间点取消是完全正常的操作，清掉的话
            #   那次取消就静悄悄地没了。
            # ★ 交接 / 被接管同理：run 还在（别的实例上）继续，锁的 owner 就是它自己。
            if not suspended and not interrupted and not slot.lost:
                with contextlib.suppress(Exception):
                    await relay.clear_cancel(run_id)
                    if thread_id is not None:
                        # owner=run_id：锁若已过期并被下一个 run 持有，这里不会误删（H3）
                        await relay.release_thread_lock(thread_id, owner=run_id)
            with contextlib.suppress(Exception):
                await redis.aclose()

    async def _checkpoint(
        self,
        run_id: UUID,
        *,
        thread_id: UUID,
        stream_thread_id: UUID,
        rows: list[StoredMessage],
        events: list[TraceEvent],
        last_seq: int,
        approval_mode: str | None = None,
        run_state: Mapping[str, Any] | None = None,
    ) -> None:
        """一个工具轮次的检查点（§5.1）：消息 + 事件归档 + 序号，一个事务。

        approval_mode 非空 = 本轮里计划获批、模式变了：与「计划已批准」那条工具结果同一个事务落库。
        run_state = 本轮里变了的图 state 键（如 todos），与写下它的那条工具结果同一个事务落库。

        ★ 第一条写是围栏（checkpoint 校验 owner）：租约已不在本实例就抛 LeaseLost、
          整个回滚 —— 接管者与假死复活的旧实例不能双写同一个 run。
        """
        async with self._sessionmaker() as session:
            records = RunRecordRepository(session)
            await records.checkpoint(run_id, owner=self._instance, last_seq=last_seq)
            if rows:
                await ThreadRepository(session).add_messages(
                    thread_id=thread_id, messages=rows, run_id=run_id
                )
            if approval_mode:
                await _save_approval_mode(session, run_id, thread_id, approval_mode)
            await records.save_state(run_id, run_state or {})
            await EventArchive(session).archive_events(events, thread_id=stream_thread_id)
            await session.commit()

    async def _persist(
        self,
        *,
        run_id: UUID,
        thread_id: UUID,
        stream_thread_id: UUID,
        events: list[TraceEvent],
        assistant_content: list[dict] | None,
        usage: dict[str, int],
        segment_end: TraceEvent | None,
        last_seq: int = 0,
        title: dict[str, Any] | None = None,
        transcript: Transcript | None = None,
        suspended: bool = False,
        approval_mode: str | None = None,
        run_state: Mapping[str, Any] | None = None,
    ) -> None:
        """一段的收尾落库。events 是**上一个检查点之后**的事件，之前的已经归档过了。

        approval_mode 非空 = 上一个检查点之后计划获批、模式变了，随收尾一起落库。
        run_state 同理：上一个检查点之后变了的图 state 键。"""
        terminal = None if suspended else segment_end
        status = _STATUS_BY_TERMINAL.get(terminal.type, "failed") if terminal else "failed"
        error_kind = None
        error_message = None
        if terminal is not None and terminal.type is EventType.RUN_FAILED:
            error_kind = terminal.data.get("error_kind")
            error_message = str(terminal.data.get("message", ""))[:2000]
        elif terminal is None and not suspended:
            error_kind = "interrupted"
            error_message = "事件流未产出终止事件"
        # 挂起时还在等的调用：事件里是数组，落库还原成 state 的形状 {tool_call_id: Wait}
        waits = (segment_end.data.get("waiting_on") or []) if suspended and segment_end else []
        waiting_on = {
            str(wait["tool_call_id"]): {"reason": wait.get("reason"), "token": wait.get("token")}
            for wait in waits
            if isinstance(wait, dict) and wait.get("tool_call_id")
        }

        async with self._sessionmaker() as session:
            records = RunRecordRepository(session)
            threads = ThreadRepository(session)
            # ★ 有 transcript 就落**完整序列**（模型说的每一轮 + 每个工具的
            #   结果），没有就退回只落最终正文。
            #
            #   两条路的分界不是「native / acp」而是「拿不拿得到忠实的序列」：
            #   acp 的工具调用发生在 Pod 内部，平台侧拼不出来，于是它走后一条
            #   （它的上下文恢复本来也不靠 message 表）。
            #
            #   ★ 落库与终态无关 —— 失败的 run 也落。模型说过的话、调过的工具
            #     是**已经发生的事实**；抹掉它们的代价是用户重试时模型不知道
            #     上次做到哪，于是把有副作用的操作又做一遍。中断的工具调用由
            #     to_storage 补上 is_error 的结果，格式始终是合法的。
            #
            # ★ 只落检查点之后的部分（remaining_rows）：之前的已经在检查点里落过了。
            #
            # ★ 挂起时还在等的调用不补「中断」—— 它们故意悬空，与下面的 run.waiting_on
            #   在**同一个事务**里落库，续跑时一起交回（contracts/suspension.py）。
            if transcript:
                await threads.add_messages(
                    thread_id=thread_id,
                    messages=transcript.remaining_rows(waiting=waiting_on),
                    run_id=run_id,
                )
            elif assistant_content:
                await threads.add_message(
                    thread_id=thread_id,
                    role="assistant",
                    content=assistant_content,
                    run_id=run_id,
                )
            if approval_mode:
                await _save_approval_mode(session, run_id, thread_id, approval_mode)
            await records.save_state(run_id, run_state or {})
            if title is not None:
                # WHERE title_source != 'manual' 在仓库里，用户改过的名字不会被覆盖
                await threads.set_generated_title(
                    thread_id,
                    title=str(title.get("title", "")),
                    degraded=bool(title.get("degraded")),
                )
            last_seq = max(last_seq, events[-1].seq if events else 0)
            # ★ 围栏（owner=本实例）：租约已被接管就抛 LeaseLost，整个收尾回滚（§13）
            if suspended:
                # ★ 「在等什么」必须落库：续跑的 barrier 按 reason 分派，只记
                #   token 的话审批挂起会被当成 SubAgent → barrier 立刻满足 → 死循环。
                await records.suspend(
                    run_id,
                    last_seq=last_seq,
                    usage=usage,
                    waiting_on=waiting_on,
                    owner=self._instance,
                )
            else:
                await records.finish(
                    run_id,
                    status=status,
                    last_seq=last_seq,
                    usage=usage,
                    error_kind=error_kind,
                    error_message=error_message,
                    owner=self._instance,
                )
            # ★ 事件逐段归档。Redis 的 Stream 有 TTL，而一个 run 可能横跨
            #   一小时以上的等待 —— 前几段的事件不当场落库的话，用户在委派
            #   返回后刷新页面，看到的是一段从中间开始的历史。
            #
            # ★ 归档按**根会话**分组（stream_thread_id），与事件流同一个口径。
            #   子 run 的事件因此归到父会话名下 —— 回放一条会话流时它们必须
            #   在里面。
            await EventArchive(session).archive_events(events, thread_id=stream_thread_id)
            if not suspended:
                # run 结束：还挂着的审批一并作废（CLI 的权限请求可能没等到撤回），弹窗随之消失
                await ApprovalService(session).expire_run(run_id)
                # 归档之后再汇总：这一段的 skill.loaded 也要算进去（跨段的 run 只算一次）
                await SkillUsageRepository(session).record_run(
                    run_id, succeeded=status == "succeeded", day=_today()
                )
            await records.touch_thread(thread_id)
            await session.commit()

    async def force_fail(self, run_id: UUID) -> None:
        """兜底：执行时发生未捕获的异常，判失败收掉，别让会话一直停在 running。"""
        async with self._sessionmaker() as session:
            await RunRecordRepository(session).finish(
                run_id,
                status="failed",
                last_seq=0,
                usage={},
                error_kind="internal_error",
                error_message="执行器内部错误",
                # 围栏：已被别的实例接管的 run 不能被这里判死
                owner=self._instance,
            )
            await ApprovalService(session).expire_run(run_id)
            await session.commit()
