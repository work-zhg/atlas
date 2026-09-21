"""委派编排：把一次 `task` 调用变成子会话上的一个子 run。

设计见 detail/subagent.html。三句话概括：

  · 子会话是 **thread**，不是 run —— 查找键 (parent_thread_id, subagent_name)
  · 一次委派是子会话上的一个 **run**，parent_run_id 指回发起者
  · 父 **await** 它到终态，取最终文本作为 ToolMessage

「父在等」不是妥协：委派的语义就是「我需要这个结论才能继续」。等的是一个
**独立可寻址**的子 run —— 它有自己的 run_id、自己的事件流、自己的审批、
自己的取消令牌，孤儿回收天然覆盖。这四样东西是「子 run」白拿到的，
图内直接 ainvoke 一个都没有。

★ engine 侧看到的只有 `DelegationProtocol`（三个标量进、文本出）。本模块是它唯一的实现，
  也是唯一知道「子会话是一行 thread」的地方。
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID, uuid4

from langgraph.config import get_stream_writer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas_server.domain.spec import AgentSpec, SubAgentSpec
from atlas_server.domain.translator import extract_text

from ..config import Settings
from ..db.models import Thread
from ..executor.base import RunExecutor
from ..repositories.approval import ApprovalRepository
from ..repositories.run import RunRepository
from ..repositories.thread import ThreadRepository
from ..stream.relay import EventRelay

logger = logging.getLogger(__name__)

__all__ = ["DelegationRejected", "SubagentService"]

_TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


class DelegationRejected(RuntimeError):
    """委派被拒。消息是**给模型看的**，必须可操作。

    ★ 拒绝要在调用的那一刻发生，不能让模型等到锁超时才知道。撞上限、
      同轮重名、子智能体不存在都归这里 —— kernel 的 task 工具把它接成
      一段错误文本回给模型，主 agent 于是能改派、合并任务或自己动手。
    """


class SubagentService:
    """一个父 run 的委派编排器。

    ★ 生命周期绑定**一次父 run**，不是进程级单例：`_inflight` 记的是
      「本轮已经派给谁了」，而「同一轮」正是一个 run。
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        executor: RunExecutor,
        relay: EventRelay,
        *,
        parent_run_id: UUID,
        parent_thread: Thread,
        parent_spec: AgentSpec,
        agent_version_id: UUID,
        seed_skills: object = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._executor = executor
        self._relay = relay
        self._parent_run_id = parent_run_id
        self._parent_thread_id = parent_thread.id
        self._agent_id = parent_thread.agent_id
        self._created_by = parent_thread.created_by
        self._spec = parent_spec
        self._agent_version_id = agent_version_id
        #: 会话创建后的技能投送回调，注入以避免本模块依赖对象存储装配。
        #: 签名 `(thread, sub_spec) -> Awaitable[None]`。
        self._seed_skills = seed_skills
        #: 本轮已在执行中的子智能体名 —— 同名并发在这里被拦住。
        self._inflight: set[str] = set()

    # ------------------------------------------------------------------ 入口

    async def delegate(self, task: str, name: str, *, fresh: bool = False) -> str:
        """`DelegationProtocol` 的唯一实现。返回子智能体的最终文本。

        签名与 `atlas_engine.contracts.DelegationProtocol` 保持一字不差 ——
        runtime_checkable 只查方法存在，参数形状靠 tests/test_seam_contracts.py
        的真实调用钉住。
        """
        sub = self._spec.subagent(name)
        if sub is None:
            known = ", ".join(s.name for s in self._spec.subagents) or "（无）"
            msg = f"子智能体 {name!r} 不存在。可用的是：{known}"
            raise DelegationRejected(msg)

        # ★ 同轮重名在这里拒，而不是等它去撞会话串行锁。撞锁的表现是
        #   「卡住直到超时」，模型既不知道原因也拿不到替代方案。
        #
        #   只对持有会话的（persistent）子智能体成立：一次性子智能体每次
        #   新建一个会话，彼此不共享状态也不争锁 —— 「并行调研五个选项」
        #   正是它存在的理由（设计 §09）。
        claimed = sub.session_mode == "persistent"
        if claimed:
            # ★ 检查与占位之间**一个 await 都不能有**。模型在一个 turn 里发
            #   两个 task 是并发执行的：先 await 准入查询再占位的话，两者都
            #   能通过检查，于是同名委派照样起了两个子 run —— 拦截形同虚设。
            if name in self._inflight:
                msg = (
                    f"{name} 已在本轮中被委派。子智能体持有会话，同名委派必须串行 —— "
                    f"把两个子任务合并成一次，或改派给不同的子智能体。"
                )
                raise DelegationRejected(msg)
            self._inflight.add(name)

        try:
            await self._check_admission(name)
            return await self._run_once(sub, task, fresh=fresh)
        finally:
            if claimed:
                self._inflight.discard(name)

    # ------------------------------------------------------------------ 准入

    async def _check_admission(self, name: str) -> None:
        async with self._sessionmaker() as session:
            runs = RunRepository(session)
            running = await runs.count_active_subruns()
            if running >= self._settings.subagent_max_running:
                msg = (
                    f"当前已有 {running} 个子智能体在执行（上限 "
                    f"{self._settings.subagent_max_running}）。稍后重试，"
                    f"或先自己处理一部分。"
                )
                raise DelegationRejected(msg)

            used = await runs.count_subruns_of(self._parent_run_id)
            if used >= self._settings.subagent_max_per_run:
                msg = (
                    f"本轮已委派 {used} 次，达到上限 "
                    f"{self._settings.subagent_max_per_run}。剩下的工作请自己完成，"
                    f"或在下一轮里继续。"
                )
                raise DelegationRejected(msg)

    # ------------------------------------------------------------------ 一次委派

    async def _run_once(self, sub: SubAgentSpec, description: str, *, fresh: bool) -> str:
        thread_id = await self._resolve_thread(sub, fresh=fresh)

        # 子会话也是 thread，会话串行锁原样适用 —— 同名委派因此必然串行。
        # 上面的 _inflight 只是把这个必然的冲突提前到调用时暴露。
        run_id = uuid4()
        ttl = max(self._spec.limits.timeout_s, self._settings.approval_timeout_s) + 120
        if not await self._relay.acquire_thread_lock(thread_id, owner=run_id, ttl_s=ttl):
            msg = (
                f"{sub.name} 的会话正被另一个 run 占用。它持有会话，同名委派必须串行 —— "
                f"稍后重试，或改派给别的子智能体。"
            )
            raise DelegationRejected(msg)

        try:
            async with self._sessionmaker() as session:
                await RunRepository(session).create(
                    run_id=run_id,
                    thread_id=thread_id,
                    agent_version_id=self._agent_version_id,
                    parent_run_id=self._parent_run_id,
                )
                await session.commit()
        except Exception:
            await self._relay.release_thread_lock(thread_id, owner=run_id)
            raise

        # 起一条投递到子会话的用户消息：子会话的历史与主线程完全分开，
        # 任务书就是它这一轮的输入（message 为事实源，执行器从表里重建）。
        async with self._sessionmaker() as session:
            await ThreadRepository(session).add_message(
                thread_id=thread_id,
                role="user",
                content=[{"type": "text", "text": description}],
                run_id=run_id,
            )
            await session.commit()

        await self._executor.submit(run_id)
        logger.info(
            "委派 %s → 子会话 %s 子 run %s（父 run %s）",
            sub.name,
            thread_id,
            run_id,
            self._parent_run_id,
        )
        return await self._await_result(sub.name, thread_id, run_id)

    # ------------------------------------------------------------------ 会话解析

    async def _resolve_thread(self, sub: SubAgentSpec, *, fresh: bool) -> UUID:
        """找到（或建出）这个子智能体的会话。

        三条分支，差别只在「查不查已有子会话」：
          persistent + 未要求 fresh → 命中就复用，这就是「恢复」
          persistent + fresh        → 归档旧的、新建一个（历史不删）
          ephemeral                 → 永远新建，跑完归档 ⇒ 可以并行
        """
        async with self._sessionmaker() as session:
            threads = ThreadRepository(session)

            if sub.session_mode == "persistent":
                existing = await threads.find_subagent(self._parent_thread_id, sub.name)
                if existing is not None:
                    if not fresh:
                        return existing.id
                    await threads.archive_subagent(existing)

            thread = await threads.create(
                agent_id=self._agent_id,
                # 标题固定：子会话不进会话列表，标题只在父会话详情页的
                # 分支上出现 —— 让它在那里一眼可辨，不值得再烧一次模型调用。
                title=f"子智能体 · {sub.name}",
                created_by=self._created_by,
                parent_thread_id=self._parent_thread_id,
                subagent_name=sub.name,
                # ★ 一次性子会话从出生就是 'ephemeral'：它因此既不被
                #   find_subagent 命中（不会被「恢复」），也不占
                #   (parent, subagent_name) 的部分唯一索引 —— 于是同一个
                #   一次性子智能体可以真并行。subagent_name 照常写入，
                #   执行器要靠它派生子智能体的 spec。
                status="ephemeral" if sub.session_mode == "ephemeral" else "active",
            )
            # ★ 技能按**子会话自己的** thread_id 投送。它挂的 workspace 是
            #   父会话的，skills 却必须是自己的（设计 §03）。
            if self._seed_skills is not None:
                await self._seed_skills(thread, sub)  # type: ignore[operator]
            await session.commit()
            return thread.id

    # ------------------------------------------------------------------ 等终态

    async def _await_result(self, name: str, thread_id: UUID, run_id: UUID) -> str:
        """轮询子 run 到终态，取子会话最后一条 assistant 消息。

        ★ 父被取消时级联取消子 run，而不是丢下它自己跑完 —— 子 run 吃的是
          同一批配额（acp 的话还占着一个 Pod）。
        """
        interval = self._settings.subagent_poll_interval_s
        deadline = asyncio.get_running_loop().time() + self._spec.limits.timeout_s
        forwarded: set[UUID] = set()

        while True:
            await asyncio.sleep(interval)

            if await self._relay.is_cancelled(self._parent_run_id):
                await self._relay.request_cancel(run_id)
                return f"{name} 的委派因父 run 被取消而中止。"

            async with self._sessionmaker() as session:
                run = await RunRepository(session).get(run_id)
                if run is None:
                    return f"{name} 的子 run 记录丢失，委派未能完成。"
                # ★ 顺手把子 run 的待审批冒泡到**父**的事件流。子 run 有自己
                #   的流，而前端只订阅父 run 那一条 —— 不冒泡的话弹窗永远不
                #   出现，子智能体卡在等人点头，直到 bridge 的 adapter 超时
                #   （实测两次委派各卡满 300s，审批最终 expired）。
                await self._forward_approvals(session, name, run_id, forwarded)
                if run.status in _TERMINAL:
                    return await self._final_text(session, name, thread_id, run)

            if asyncio.get_running_loop().time() > deadline:
                await self._relay.request_cancel(run_id)
                return (
                    f"{name} 超过 {self._spec.limits.timeout_s}s 仍未完成，已请求取消。"
                    f"任务可能过大 —— 拆成更小的一步再派。"
                )

    async def _forward_approvals(
        self, session: AsyncSession, name: str, run_id: UUID, forwarded: set[UUID]
    ) -> None:
        """把子 run 的待审批转成父流上的 approval.required。

        ★ 走 custom stream 通道，与 engine 的 ApprovalMiddleware 同一条路
          （runner 的 _CUSTOM_EVENTS 负责翻译）—— 本方法跑在 task 工具里，
          也就是在图内部，够不到 runner 的事件工厂。

        ★ data 里带 **run_id**：决策要 POST 到**子** run 的端点，而父流上的
          其它审批都属于父 run。前端据此选择提交目标，缺省才用当前流的 run。

        ★ 只发不撤：审批被决定或过期之后，父流上那条事件仍然在（事件流只增
          不改）。前端本来就按 approval_id 维护「已决策」集合来关弹窗。
        """
        pending = await ApprovalRepository(session).list_pending(run_id)
        fresh = [a for a in pending if a.id not in forwarded]
        if not fresh:
            return
        try:
            writer = get_stream_writer()
        except Exception:  # 不在图上下文里就没有 writer
            logger.warning("拿不到 stream writer，子智能体的审批无法冒泡到父流")
            return
        for approval in fresh:
            forwarded.add(approval.id)
            writer(
                {
                    "kind": "approval.required",
                    "approval_id": str(approval.id),
                    "tool_name": approval.tool_name,
                    "args": approval.args,
                    "reason": f"子智能体 {name} 请求执行该工具",
                    "run_id": str(run_id),
                }
            )

    async def _final_text(
        self, session: AsyncSession, name: str, thread_id: UUID, run: object
    ) -> str:
        status = run.status  # type: ignore[attr-defined]
        if status != "succeeded":
            detail = (run.error_message or "").strip()  # type: ignore[attr-defined]
            # ★ 失败照实说，不返回一段像结论的话。返回空串或含糊措辞的话，
            #   主 agent 会把「子智能体没说什么」当成「没有发现」继续往下走。
            return f"{name} 的委派以 {status} 结束。{detail}".strip()

        content = await RunRepository(session).last_assistant_content(thread_id)
        text = extract_text(content).strip() if content else ""
        return text or f"{name} 完成了委派但没有产出文本结论。"
