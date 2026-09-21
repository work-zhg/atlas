"""进程内 RunExecutor（文档 §12.1）—— 只负责执行循环。

「一轮怎么跑」在 runtime.py（AgentRuntime）：本文件的前后两段——载入/标记
运行中，与收事件/落库/释放锁——与 agent 类型无关，acp 接进来时一行不改
（acp 详设 §02）。能力注入的组装在 assembly.py（HookAssembly）：换
arq/Celery worker 时搬走的是本文件的循环，组装与 runtime 原样复用。

代价是明确的、不藏着的：部署/重启会中断进行中的 run。对策是启动时扫描
把残留的 running/queued 标记为 interrupted（见 RunService.reap_orphans），
外加 SIGTERM 后的 graceful 窗口。

后台任务**必须自建 session 与 Redis 连接** —— 创建它的那个 HTTP 请求早已结束，
其 session 也已关闭。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from atlas_engine.kernel.middleware.compaction import SUMMARY_PREFIX
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas_server.domain.events import EventType, TraceEvent
from atlas_server.domain.spec import AgentSpec, spec_for_subagent
from atlas_server.domain.translator import extract_text

from ..config import Settings
from ..db.models import AgentVersion, Message, Run, Thread
from ..memory import build_job, enqueue
from ..redisx import make_redis
from ..repositories.run import RunRepository
from ..repositories.thread import ThreadRepository
from ..schemas.agent import AgentSpecIn
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


def _to_lc(message: Message) -> BaseMessage:
    content: Any = message.content
    if message.role == "assistant":
        return AIMessage(content=content)
    return HumanMessage(content=content)


def _spec_from_version(version: AgentVersion, *, slug: str, name: str) -> AgentSpec:
    return AgentSpecIn.model_validate(version.spec).to_engine(slug=slug, name=name)


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

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ------------------------------------------------------------------ 执行

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

        try:
            async with self._sessionmaker() as session:
                runs = RunRepository(session)
                loaded = await runs.load_for_execution(run_id)
                if loaded is None:
                    logger.error("run %s 不存在，跳过", run_id)
                    return
                run, thread, version = loaded
                thread_id = thread.id

                prepared = await self._prepare(session, run, thread, version)
                await runs.mark_running(run_id)
                await session.commit()

            assistant_content: list[dict] | None = None
            usage: dict[str, int] = {}
            terminal: TraceEvent | None = None
            title: dict[str, Any] | None = None

            # ★ 「一轮怎么跑」在 runtime 里（acp 详设 §02）。本方法的前后两段
            #   ——载入/标记运行中，与收事件/落库/释放锁——与 agent 类型无关，
            #   acp 接进来时一行都不用改。
            runtime = select_runtime(prepared, native=self._native, acp=self._acp)
            # ★ Run 是 trace 的根（可观测性设计 §04）。子 span 由事件流派生 ——
            #   native 与 acp 产出同形事件，所以这一处覆盖两条执行路径。
            #   未启用遥测时 RunTrace 里拿到的是 no-op span，零开销。
            with RunTrace.start(prepared, run_id=str(run_id)) as run_trace:
                async for event in runtime.run_turn(
                    prepared, run_id=run_id, redis=redis, relay=relay
                ):
                    run_trace.observe(event)
                    collected.append(event)

                    # ★ 终止事件**先落库再发布**。否则客户端收到 run.finished 后
                    #   立刻 GET /runs/{id} 会读到 status=running、tokens=0 ——
                    #   而前端正是这么用的（收到终止事件就去取最终用量）。
                    #   这条顺序保证："看到终止事件" ⇒ "DB 已是最终状态"。
                    if event.is_terminal:
                        terminal = event
                        break

                    await relay.publish(event)
                    if event.type is EventType.MESSAGE_COMPLETED:
                        assistant_content = event.data.get("content")
                    elif event.type is EventType.USAGE_UPDATED:
                        usage = {k: v for k, v in event.data.items() if isinstance(v, int)}
                    elif event.type is EventType.TITLE_GENERATED:
                        title = dict(event.data)

            await self._persist(
                run_id=run_id,
                thread_id=thread_id,
                events=collected,
                assistant_content=assistant_content,
                usage=usage,
                terminal=terminal,
                title=title,
            )
            if terminal is not None:
                await relay.publish(terminal)

            # ★ 记忆抽取（记忆设计 §04）。放在**发布终止事件之后** ——
            #   它是 run 的下游消费者，绝不该让用户多等一毫秒。
            #
            # ★ 只入队，不在这里抽取：抽取要调一次 LLM，同步做会把 run 的
            #   收尾拖长。入队本身也吞异常（queue.enqueue 内部兜着）——
            #   记忆服务挂了，用户照常能发消息（§11）。
            await self._remember(prepared, run_id=run_id, terminal=terminal,
                                 assistant_content=assistant_content, redis=redis)
        finally:
            with contextlib.suppress(Exception):
                await relay.clear_cancel(run_id)
                if thread_id is not None:
                    # owner=run_id：锁若已过期并被下一个 run 持有，这里不会误删（H3）
                    await relay.release_thread_lock(thread_id, owner=run_id)
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

        # 最后一条用户消息是本轮输入，其余是历史
        last = rows[-1]
        history: list[BaseMessage] = [_to_lc(m) for m in rows[:-1]]

        # §7.4：已压缩的早期消息以摘要形式回填，而不是重新送原文。
        # 没有这一步，每个 run 都要重压一次 —— 摘要 token 重复付、
        # prompt cache 每轮击穿（§7.3 坑 2 的抖动）。
        if thread.summary:
            history.insert(0, AIMessage(content=SUMMARY_PREFIX + thread.summary))

        return PreparedRun(
            spec=spec,
            input_content=last.content,
            history=history,
            history_upto=rows[-2].created_at if len(rows) > 1 else None,
            thread=thread,
            agent_version_id=version.id,
        )

    async def _persist(
        self,
        *,
        run_id: UUID,
        thread_id: UUID,
        events: list[TraceEvent],
        assistant_content: list[dict] | None,
        usage: dict[str, int],
        terminal: TraceEvent | None,
        title: dict[str, Any] | None = None,
    ) -> None:
        status = _STATUS_BY_TERMINAL.get(terminal.type, "failed") if terminal else "failed"
        error_kind = None
        error_message = None
        if terminal is not None and terminal.type is EventType.RUN_FAILED:
            error_kind = terminal.data.get("error_kind")
            error_message = str(terminal.data.get("message", ""))[:2000]
        elif terminal is None:
            error_kind = "interrupted"
            error_message = "事件流未产出终止事件"

        async with self._sessionmaker() as session:
            runs = RunRepository(session)
            threads = ThreadRepository(session)
            if assistant_content:
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
            await runs.finish(
                run_id,
                status=status,
                last_seq=events[-1].seq if events else 0,
                usage=usage,
                error_kind=error_kind,
                error_message=error_message,
            )
            await runs.archive_events(events)
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
