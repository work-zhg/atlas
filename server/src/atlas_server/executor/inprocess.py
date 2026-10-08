"""进程内 RunExecutor（文档 §12.1）—— 只负责执行循环。

「一轮怎么跑」在 runtime.py（AgentRuntime）：本文件的前后两段——载入/标记
运行中，与收事件/落库/释放锁——与 agent 类型无关，acp 接进来时一行不改
（acp 详设 §02）。能力注入的组装在 assembly.py（HookAssembly）：换
arq/Celery worker 时搬走的是本文件的循环，组装与 runtime 原样复用。

代价是明确的、不藏着的：部署/重启时进程内的执行会停下。启动时**不**把残留的
running/queued 标记为 interrupted —— 进行中的 run 应当断联恢复，而不是直接判中断
（恢复尚未实现：在那之前它们会停在 running，需要人工处理）。

后台任务**必须自建 session 与 Redis 连接** —— 创建它的那个 HTTP 请求早已结束，
其 session 也已关闭。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from atlas_engine.contracts import parse_suspension, released_marker
from atlas_engine.kernel.middleware.compaction import SUMMARY_PREFIX
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas_server.domain.events import EventFactory, EventType, TraceEvent
from atlas_server.domain.messages import KIND_CHAT, Transcript, from_storage
from atlas_server.domain.spec import AgentSpec, spec_for_subagent
from atlas_server.domain.translator import extract_text

from ..config import Settings
from ..db.models import AgentVersion, Message, Run, Thread
from ..memory import build_job, enqueue
from ..redisx import make_redis
from ..repositories.approval import ApprovalRepository
from ..repositories.run import SUSPENDED_STATUSES, RunRepository
from ..repositories.skill_usage import SkillUsageRepository
from ..repositories.thread import ThreadRepository
from ..schemas.agent import AgentSpecIn
from ..services.subagent import resolve_delegation
from ..stream.relay import EventRelay
from ..telemetry.run_trace import RunTrace
from .assembly import HookAssembly, ModelBuilder, PreparedRun, default_model_builder
from .runtime import NativeRuntime, select_runtime

logger = logging.getLogger(__name__)

_STATUS_BY_TERMINAL = {
    EventType.RUN_FINISHED: "succeeded",
    EventType.RUN_FAILED: "failed",
    EventType.RUN_CANCELLED: "cancelled",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _last_chat_input(rows: list[Message]) -> int | None:
    """最后一条 kind='chat' 的用户消息在 rows 里的下标。None = 一条都没有。"""
    for index in range(len(rows) - 1, -1, -1):
        if rows[index].role == "user" and rows[index].kind == KIND_CHAT:
            return index
    return None


def _spec_from_version(version: AgentVersion, *, slug: str, name: str) -> AgentSpec:
    return AgentSpecIn.model_validate(version.spec).to_engine(slug=slug, name=name)


def _today() -> date:
    return datetime.now(UTC).date()


class InProcessExecutor:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        model_builder: ModelBuilder | None = None,
        acp_runtime: Any = None,
        memory: Any = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        # 可注入：单测塞一个假模型，整条执行链路即可脱离网络运行
        self._build_model = model_builder or default_model_builder
        self._assembly = HookAssembly(
            sessionmaker,
            settings,
            self._build_model,
            # 委派要能起子 run —— 它走的就是本执行器的 submit，与用户发起的
            # run 完全同一条链路（设计 §06）。
            executor=self,
            # 只读的 search_memory 工具要它（记忆设计 §06 辅路径）
            memory=memory,
        )
        #: 进程级复用：assembly 持有实例态（沙箱/编码后端），每轮新建会把
        #: 那些状态打散。每轮变化的东西全在 run_turn 的参数里。
        self._native = NativeRuntime(self._assembly)
        #: acp 执行环境。None = 本部署没接 Pod —— kind="acp" 的 agent 会在
        #: select_runtime 里明确报错，而不是悄悄按 native 跑。
        self._acp = acp_runtime
        #: 记忆客户端。None = 没开记忆 —— _remember 直接返回，零开销。
        #: 不回落任何假实现：那会让上层以为在记，而实际什么都没记。
        self._memory = memory
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        #: 补唤醒的周期任务。None = 没起（单测里通常不需要）。
        self._sweeper: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ 协议

    async def submit(self, run_id: UUID) -> None:
        # ★ 必须给一个**全新的** contextvars.Context。
        #
        #   委派让 submit 第一次可能在「另一个 run 的执行上下文里」被调用
        #   （父 run 的 task 工具起子 run）。asyncio.create_task 默认拷贝当前
        #   上下文，而 langgraph 的流式输出与 langsmith 的追踪都挂在 contextvar
        #   上 —— 继承过去的话子 run 的 token 会被写进**父 run 的**流通道，
        #   子 run 自己的 message.delta 一个都不产出，最终落库的 assistant
        #   消息是空的。症状极具迷惑性：子 run 状态 succeeded、模型确实被调用、
        #   只是内容凭空消失。
        #
        #   语义上这也正是对的：子 run 是独立可寻址的执行，不该继承发起者的
        #   任何环境。
        task = asyncio.create_task(
            self._guarded(run_id), name=f"run:{run_id}", context=contextvars.Context()
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(run_id, None))

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
            await self._settle_cancelled(run_id)

    async def shutdown(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sweeper
            self._sweeper = None
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ------------------------------------------------------------------ 补唤醒

    def start_sweeper(self) -> None:
        """起周期扫描，把丢了唤醒信号的 suspended run 捞回来。

        ★ 为什么不能只靠子 run 主动唤醒：那个唤醒发生在子 run 收尾的代码里，
          而进程可能正好在那一刻被 SIGKILL。信号没了之后**没有任何其它机制**
          会再碰这个父 run。这个扫描是它唯一的出路。

        ★ 幂等靠 claim_for_resume 的 CAS：扫描与主动唤醒撞上时只有一个能赢。
        """
        if self._sweeper is not None:
            return
        self._sweeper = asyncio.create_task(self._sweep_forever(), name="suspended-sweeper")

    async def _sweep_forever(self) -> None:
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
        """扫一遍挂起的 run，能续的续上。返回续了几个。

        ★ 不判超时：审批只由人决定，等多久都不替用户按拒绝处理。
        """
        async with self._sessionmaker() as session:
            candidates = await RunRepository(session).suspended_runs()
        resumed = 0
        for run_id in candidates:
            if await self.resume_if_ready(run_id):
                resumed += 1
        if resumed:
            logger.info("补唤醒了 %d 个等待中的 run", resumed)
        return resumed

    def _redis(self) -> aioredis.Redis:
        return make_redis(self._settings)

    async def _guarded(self, run_id: UUID) -> None:
        try:
            await self._execute(run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 兜底：绝不让后台任务静默死掉，否则会话会永远停在 running
            logger.exception("run %s 执行时发生未捕获异常", run_id)
            with contextlib.suppress(Exception):
                await self._force_fail(run_id)

    async def _execute(self, run_id: UUID) -> None:
        redis = self._redis()
        relay = EventRelay(redis, ttl_s=self._settings.run_events_ttl_s)
        collected: list[TraceEvent] = []
        # ★ 必须先初始化：load_for_execution 返回 None 时直接 return，
        #   而 return 要经过 finally —— 那里会读它。
        thread_id: UUID | None = None
        parent_run_id: UUID | None = None
        #: 这一段是挂起收尾的（还在等子 run），不是这一轮结束了。
        #: finally 里据此**不**释放会话锁、不清取消位 —— run 还没完。
        suspended = False

        try:
            async with self._sessionmaker() as session:
                runs = RunRepository(session)
                loaded = await runs.load_for_execution(run_id)
                if loaded is None:
                    logger.error("run %s 不存在，跳过", run_id)
                    return
                run, thread, version = loaded
                thread_id = thread.id
                parent_run_id = run.parent_run_id
                # ★ 事件流归属**根会话**：子 run 的事件也进父会话那条流，
                #   否则「一个会话看到所有事」不成立（见 Thread.stream_thread_id）。
                stream_thread_id = thread.stream_thread_id
                # Redis 的序号 key 丢失时的恢复水位。每段查一次，不是每事件。
                seq_floor = await runs.thread_seq_floor(stream_thread_id)

                prepared = await self._prepare(session, run, thread, version)
                await runs.mark_running(run_id)
                await session.commit()

            assistant_content: list[dict] | None = None
            usage: dict[str, int] = {}
            #: 这一段的收尾事件 —— 终态（run 结束）或 run.suspended（还要续）。
            segment_end: TraceEvent | None = None
            title: dict[str, Any] | None = None
            # ★ 本轮实际产生的消息（模型的 tool_use、工具的结果）。落库它们
            #   是分段执行的前提 —— 段与段之间没有别的载体（§7.4 的「没有
            #   checkpointer」在这里仍然成立：事实源始终是 message 表）。
            transcript = Transcript()

            # ★ 「一轮怎么跑」在 runtime 里（acp 详设 §02）。本方法的前后两段
            #   ——载入/标记运行中，与收事件/落库/释放锁——与 agent 类型无关，
            #   acp 接进来时一行都不用改。
            runtime = select_runtime(prepared, native=self._native, acp=self._acp)
            # ★ Run 是 trace 的根（可观测性设计 §04）。子 span 由事件流派生 ——
            #   native 与 acp 产出同形事件，所以这一处覆盖两条执行路径。
            #   未启用遥测时 RunTrace 里拿到的是 no-op span，零开销。
            with RunTrace.start(
                prepared,
                run_id=str(run_id),
                parent_run_id=str(parent_run_id) if parent_run_id else None,
            ) as run_trace:
                async for event in runtime.run_turn(
                    prepared, run_id=run_id, redis=redis, relay=relay, transcript=transcript
                ):
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
                        break

                    # publish 的返回值必须用上 —— 会话级序号是它盖的
                    collected.append(
                        await relay.publish(
                            event, thread_id=stream_thread_id, floor=seq_floor
                        )
                    )
                    if event.type is EventType.MESSAGE_COMPLETED:
                        assistant_content = event.data.get("content")
                    elif event.type is EventType.USAGE_UPDATED:
                        usage = {k: v for k, v in event.data.items() if isinstance(v, int)}
                    elif event.type is EventType.TITLE_GENERATED:
                        title = dict(event.data)

            suspended = (
                segment_end is not None and segment_end.type is EventType.RUN_SUSPENDED
            )

            await self._persist(
                run_id=run_id,
                thread_id=thread_id,
                stream_thread_id=stream_thread_id,
                events=collected,
                assistant_content=assistant_content,
                usage=usage,
                segment_end=segment_end,
                title=title,
                transcript=transcript,
                suspended=suspended,
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
                await self.resume_if_ready(run_id)
                return

            # ★ 记忆抽取（记忆设计 §04）。放在**发布终止事件之后** ——
            #   它是 run 的下游消费者，绝不该让用户多等一毫秒。
            #
            # ★ 只入队，不在这里抽取：抽取要调一次 LLM，同步做会把 run 的
            #   收尾拖长。入队本身也吞异常（queue.enqueue 内部兜着）——
            #   记忆服务挂了，用户照常能发消息（§11）。
            await self._remember(prepared, run_id=run_id, terminal=segment_end,
                                 assistant_content=assistant_content, redis=redis)

            # ★ 这是个**子 run**，而且刚跑完 —— 去叫醒等着它的父 run。
            #   放在最后：父续跑会起一个新任务，不该让它排在本 run 的收尾前面。
            if parent_run_id is not None:
                await self.resume_if_ready(parent_run_id)
        finally:
            # ★ suspend 不释放会话锁、不清取消位 —— 这一轮没结束。
            #
            #   锁要一直拿着：放了的话用户能在等待期间发第二条消息，两个 run
            #   交错写同一个 thread，「同一会话串行」就破了。
            #   取消位要留着：用户在等待期间点取消是完全正常的操作，清掉的话
            #   那次取消就静悄悄地没了。
            if not suspended:
                with contextlib.suppress(Exception):
                    await relay.clear_cancel(run_id)
                    if thread_id is not None:
                        # owner=run_id：锁若已过期并被下一个 run 持有，这里不会误删（H3）
                        await relay.release_thread_lock(thread_id, owner=run_id)
            with contextlib.suppress(Exception):
                await redis.aclose()

    async def resume_if_ready(self, run_id: UUID) -> bool:
        """等的东西都到了就把这个 suspended 的 run 续上。返回是否真的续了。

        ★ 三个调用点，同一套判据：子 run 跑完时、父自己挂起落库后（关竞态
          窗口）、周期 reaper 补扫时。做成一个方法是因为这三处漏掉任何一个
          都会让 run 永久卡在 suspended，而那是最难发现的故障形态 ——
          没有报错、没有事件，只是一个永远转圈的界面。

        ★ CAS 在 claim_for_resume 里。几个子 run 同时结束时它们都会走到这，
          只有一个能抢到。
        """
        async with self._sessionmaker() as session:
            runs = RunRepository(session)
            run = await runs.get(run_id)
            # ★ 两个挂起态都要认。等审批的 run 落的是 awaiting_approval
            #   （suspended 的细分，为了让 UI 区分「等你」和「等机器」）——
            #   只认字面量 "suspended" 的话审批永远唤不醒，用户点了「允许」
            #   之后 run 一动不动。
            if run is None or run.status not in SUSPENDED_STATUSES:
                return False
            if not await self._waits_are_settled(session, run):
                return False
            if not await runs.claim_for_resume(run_id):
                return False  # 别人抢到了
            await session.commit()

        # ★ 用户在等待期间点了取消 —— 别再装一次图、起一次任务去发现这件事。
        #   续跑段的 runner 循环当然也会检查取消位，但那要等到第一次 astream
        #   迭代，中间白装配一次图（MCP 解析、对象存储连接、模型构造）。
        if await self._cancel_requested(run_id):
            logger.info("run %s 在等待期间被取消，不再续跑", run_id)
            await self._settle_cancelled(run_id)
            return False

        logger.info("run %s 的委派已全部返回，续跑", run_id)
        await self.submit(run_id)
        return True

    async def _waits_are_settled(self, session: AsyncSession, run: Run) -> bool:
        """这个 run 等的东西都有结果了吗 —— 续跑的 barrier。

        ★ 按 reason 分派，不是一套判据打天下：

            delegation  子 run 进终态
            approval    approval 行被决策（approved / rejected / expired）

          用委派那套去判审批的话，barrier 会**立刻满足**（审批挂起时没有子
          run），于是续跑 → 又挂起 → 再续跑，死循环。这是 run.waiting_on
          那一列存在的全部理由（迁移 0012）。

        ★ waiting_on 为空时回落到「查子 run」。0012 之前挂起的 run 没有这一列，
          而它们只可能是委派挂起 —— 回落让那些 run 不至于卡死。
        """
        runs = RunRepository(session)
        waits = run.waiting_on or []
        if not waits:
            # ★ 没有 waiting_on 却是 awaiting_approval = **acp 的在线等待**，
            #   run 其实还在跑（bridge 正等着那个权限响应）。续跑它等于把同一个
            #   run 执行两遍：第二条上游连接 会把第一个 superseded(4409)，
            #   整轮崩掉。这条与 suspended_runs 的过滤是同一个判据，两处都守
            #   —— 主动唤醒那一路不经过扫描。
            if run.status == "awaiting_approval":
                return False
            return not await runs.has_unfinished_children(run.id)

        approvals = ApprovalRepository(session)
        for wait in waits:
            reason = (wait or {}).get("reason")
            token = (wait or {}).get("token") or ""
            if reason == "approval":
                if not await self._approval_settled(approvals, token):
                    return False
            elif reason == "delegation":
                if await runs.has_unfinished_children(run.id):
                    return False
            else:
                # 不认识的 reason —— 不敢续跑（可能永远等不到），也不敢判死。
                # 记一条日志让它被发现，而不是静默卡住。
                logger.warning("run %s 的 waiting_on 里有未知 reason：%r", run.id, reason)
                return False
        return True

    @staticmethod
    async def _approval_settled(approvals: ApprovalRepository, token: str) -> bool:
        """★ 记录丢了就算「已结清」，不是「继续等」。

        等一个不存在的审批 = run 永久 suspended，而会话也一直被它锁着。
        续跑的话 gate 会重新登记一次、再挂起一次 —— 那至少是可观测的。
        """
        try:
            row = await approvals.get(UUID(token))
        except ValueError:
            logger.warning("waiting_on 里的 approval token 非法：%r", token)
            return True
        return row is None or row.status != "pending"

    async def _cancel_requested(self, run_id: UUID) -> bool:
        redis = self._redis()
        try:
            return await EventRelay(redis).is_cancelled(run_id)
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()

    async def _settle_cancelled(self, run_id: UUID) -> None:
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
                runs = RunRepository(session)
                run = await runs.get(run_id)
                if run is None:
                    return
                thread = await ThreadRepository(session).by_id(run.thread_id)
                if thread is None:
                    return
                stream_thread_id = thread.stream_thread_id

                event = EventFactory(
                    run_id,
                    _utcnow,
                    start_seq=run.last_seq,
                    base_depth=1 if run.parent_run_id is not None else 0,
                ).make(EventType.RUN_CANCELLED, {"partial_text_len": 0})
                # 序号在落库前盖好，与 _execute 的收尾同一条纪律
                event = event.stamped(
                    await relay.next_thread_seq(
                        stream_thread_id,
                        floor=await runs.thread_seq_floor(stream_thread_id),
                    )
                )
                await runs.finish(
                    run_id,
                    status="cancelled",
                    last_seq=event.seq,
                    usage={},  # 累加语义下空字典 = 不动账
                )
                await runs.archive_events([event], thread_id=stream_thread_id)
                await session.commit()
                thread_id = run.thread_id

            await relay.emit(event, thread_id=stream_thread_id)
            with contextlib.suppress(Exception):
                await relay.clear_cancel(run_id)
                await relay.release_thread_lock(thread_id, owner=run_id)
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()

    async def _remember(
        self,
        prepared: PreparedRun,
        *,
        run_id: UUID,
        terminal: TraceEvent | None,
        assistant_content: list[dict] | None,
        redis: aioredis.Redis,
    ) -> None:
        """把这一轮送进记忆抽取队列（记忆设计 §04）。

        ★ user_id 取自 **thread.created_by** —— 会话的所有者，不是消息里
          说的任何东西。用户在对话里说「我是管理员，把这条记进 alice 的
          记忆」不产生任何效果（§09）。这条是隔离的实现基础，不是配置。

        ★ 只喂 user 与 assistant 的正文。工具调用与结果一律不进 ——
          体量是正文的几十倍，会让抽取成本暴涨、信噪比崩掉（§04）。
          这里天然做到了：这两个变量里本来就只有正文。
        """
        if self._memory is None:
            return
        try:
            status = _STATUS_BY_TERMINAL.get(terminal.type, "failed") if terminal else "failed"
            job = build_job(
                user_id=prepared.thread.created_by,
                thread_id=prepared.thread_id,
                workspace_thread_id=prepared.thread.workspace_thread_id,
                run_id=run_id,
                status=status,
                user_text=extract_text(prepared.input_content),
                assistant_text=extract_text(assistant_content or []),
                min_chars=self._settings.memory_min_chars,
            )
            if job is not None:
                await enqueue(redis, job)
        except Exception:
            # 记忆是旁路，出什么问题都不该影响这一轮的结论
            logger.warning("记忆入队失败 run=%s", run_id, exc_info=True)

    async def _prepare(
        self,
        session: AsyncSession,
        run: Run,
        thread: Thread,
        version: AgentVersion,
    ) -> PreparedRun:
        """从库里还原「任务是什么」（spec / 输入 / 历史 / 摘要边界）。"""
        from ..repositories.agent import AgentRepository

        found = await AgentRepository(session).get(thread.agent_id)
        # agent 行理论上必然存在（thread.agent_id 有外键），兜底只为不炸
        slug, name = (found[0].slug, found[0].name) if found is not None else ("agent", "agent")

        spec = _spec_from_version(version, slug=slug, name=name)

        # ★ 子会话的 run：从父 agent 的同一份快照里派生出子智能体自己的
        #   AgentSpec。子会话因此走与普通 run **完全同一条**链路 ——
        #   历史重建、压缩、步数刹车、审批、取消、孤儿回收全部原样复用。
        if thread.subagent_name:
            spec = spec_for_subagent(spec, thread.subagent_name)

        rows = await RunRepository(session).history(thread.id, after=thread.summary_upto)
        if not rows:
            return PreparedRun(
                spec=spec,
                input_content="",
                thread=thread,
                agent_version_id=version.id,
            )

        # ★ 本轮输入是最后一条 **kind='chat' 的用户消息**，不是最后一条消息。
        #   工具结果在 Anthropic 的格式里同样是 role='user'（domain/messages.py），
        #   不加区分就会把一批工具结果当成用户的新提问 —— 表现是模型莫名回答
        #   一段 JSON，而用户真正的问题被归进了历史。
        input_index = _last_chat_input(rows)

        # ★ 输入消息之后还有东西 = 这个 run 之前已经跑过一段（委派挂起后续跑）。
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

        # ★ 把上一段留下的哨兵换成子智能体真正的结论。
        #
        #   这是续跑的**全部**秘密：挂起时落库的那条 ToolMessage 带着正确的
        #   tool_call_id（工具节点自己生成的），内容是哨兵。在这里换掉内容，
        #   模型看到的就是一次普通的、已经拿到结果的工具调用 —— 它完全不知道
        #   中间隔了一小时和一次进程重启。
        #
        #   ★ 必须在这里做，不能留给模型看见。哨兵是内部标记，模型会把它当成
        #     子智能体说的话，然后一本正经地汇报一个不存在的结果。
        await self._settle_pending(session, history)

        # §7.4：已压缩的早期消息以摘要形式回填，而不是重新送原文。
        # 没有这一步，每个 run 都要重压一次 —— 摘要 token 重复付、
        # prompt cache 每轮击穿（§7.3 坑 2 的抖动）。
        if thread.summary:
            history.insert(0, AIMessage(content=SUMMARY_PREFIX + thread.summary))

        return PreparedRun(
            spec=spec,
            input_content=input_content,
            history=history,
            history_upto=upto,
            thread=thread,
            agent_version_id=version.id,
            resume=resume,
            start_seq=run.last_seq,
            prior_tokens=run.total_tokens,
            # ★ 子 run 的事件带 depth=1。判据就是「它是不是一次委派产生的」——
            #   委派深度结构性封顶 1，所以不用递归求深度。
            base_depth=1 if run.parent_run_id is not None else 0,
        )

    async def _settle_pending(
        self, session: AsyncSession, history: list[BaseMessage]
    ) -> None:
        """就地处理历史里的挂起哨兵。两种 reason 的处理**相反**：

            delegation  换成子智能体的结论      → 流向 model
            approval    换成「已放行」占位        → before_model 删它并跳 tools

        ★ 两者都**换内容**，不删条目：交给图的序列必须配对完整，否则
          PatchToolCallsMiddleware 会在图入口把悬空补成「was cancelled」。

        ★ 就地改 history，不改 message 表。表里留着哨兵是**对的**：它是
          「当时发生了什么」的忠实记录（这一段确实是在等待中结束的），而
          结论的权威住在子 run 自己的会话里。两边都写一份就有了两个事实源，
          而它们会在子 run 被重跑时分叉。
        """
        for index, message in enumerate(history):
            if not isinstance(message, ToolMessage):
                continue
            found = parse_suspension(message.content)
            if found is None:
                continue
            if found.reason == "approval":
                # ★ 审批的哨兵换成「已放行」占位，**不能删整条**。
                #
                #   删掉的话那个 tool_use 就悬空了，而 kernel 的
                #   PatchToolCallsMiddleware 在图入口会给一切悬空的 tool_call
                #   补上一句「was cancelled」—— 它排在核心栈里，顺序无法调整。
                #   于是 SuspensionMiddleware 看到的序列配对完整，不跳 tools，
                #   流向模型，模型看到「工具被取消了」就重新发一次调用，再挂起
                #   一次：**死循环**（真机验证时撞到的）。
                #
                #   所以这里交出去的序列必须是**配对完整**的。制造悬空的时机推到
                #   before_model —— 那时 patch 已经跑完。
                #
                #   委派相反：结论已经有了，换成真话就行（见下）。
                history[index] = ToolMessage(
                    content=released_marker(found.token),
                    tool_call_id=message.tool_call_id,
                    name=message.name,
                )
                continue
            if found.reason != "delegation":
                logger.warning("历史里有未知 reason 的哨兵：%r", found.reason)
                continue
            try:
                text = await resolve_delegation(session, UUID(found.token))
            except ValueError:  # 哨兵里不是个合法 UUID —— 不该发生
                logger.warning("哨兵里的 sub_run_id 非法：%r", found.token)
                text = "子智能体的委派记录无法解析，本轮拿不到它的结论。"
            history[index] = ToolMessage(
                content=text,
                tool_call_id=message.tool_call_id,
                name=message.name,
                status=message.status,
            )

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
        title: dict[str, Any] | None = None,
        transcript: Transcript | None = None,
        suspended: bool = False,
    ) -> None:
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

        async with self._sessionmaker() as session:
            runs = RunRepository(session)
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
            if transcript:
                await threads.add_messages(
                    thread_id=thread_id,
                    messages=transcript.to_storage(),
                    run_id=run_id,
                )
            elif assistant_content:
                await threads.add_message(
                    thread_id=thread_id,
                    role="assistant",
                    content=assistant_content,
                    run_id=run_id,
                )
            if title is not None:
                # WHERE title_source != 'manual' 在仓库里，用户改过的名字不会被覆盖
                await threads.set_generated_title(
                    thread_id,
                    title=str(title.get("title", "")),
                    degraded=bool(title.get("degraded")),
                )
            last_seq = events[-1].seq if events else 0
            if suspended:
                # ★ 「在等什么」必须落库：续跑的 barrier 按 reason 分派，只记
                #   token 的话审批挂起会被当成委派 → barrier 立刻满足 → 死循环。
                waiting_on = (segment_end.data.get("waiting_on") or []) if segment_end else []
                await runs.suspend(
                    run_id, last_seq=last_seq, usage=usage, waiting_on=waiting_on
                )
            else:
                await runs.finish(
                    run_id,
                    status=status,
                    last_seq=last_seq,
                    usage=usage,
                    error_kind=error_kind,
                    error_message=error_message,
                )
            # ★ 事件逐段归档。Redis 的 Stream 有 TTL，而一个 run 可能横跨
            #   一小时以上的等待 —— 前几段的事件不当场落库的话，用户在委派
            #   返回后刷新页面，看到的是一段从中间开始的历史。
            #
            # ★ 归档按**根会话**分组（stream_thread_id），与事件流同一个口径。
            #   子 run 的事件因此归到父会话名下 —— 回放一条会话流时它们必须
            #   在里面。
            await runs.archive_events(events, thread_id=stream_thread_id)
            if not suspended:
                # 归档之后再汇总：这一段的 skill.loaded 也要算进去（跨段的 run 只算一次）
                await SkillUsageRepository(session).record_run(
                    run_id, succeeded=status == "succeeded", day=_today()
                )
            await runs.touch_thread(thread_id)
            await session.commit()

    async def _force_fail(self, run_id: UUID) -> None:
        async with self._sessionmaker() as session:
            await RunRepository(session).finish(
                run_id,
                status="failed",
                last_seq=0,
                usage={},
                error_kind="internal_error",
                error_message="执行器内部错误",
            )
            await session.commit()
