"""SubAgent 的 server 侧：把一次 `task` 调用变成 SubAgent 会话上的一个子 run，
以及把子 run 的结局整形成交给父模型的结论文本。

设计见 detail/subagent.html。三句话概括：

  · SubAgent 的会话是 **thread**，不是 run —— 查找键 (parent_thread_id, subagent_name)
  · 一次 `task` 是这个会话上的一个 **run**，parent_run_id 指回派出它的 run
  · 父 run **不等**：spawn 立刻返回子 run id，父 run 挂起；子 run 跑完叫醒它，
    续跑时把结论交回（contracts/suspension.py，与审批同一条路）

「父在等结论」是语义，不是进程状态：等的是一个**独立可寻址**的子 run —— 它有
自己的 run_id、自己的事件、自己的审批、自己的取消令牌，孤儿回收天然覆盖。
SubAgent 也能继续派 SubAgent，这条路一层层叠上去：孙 run 叫醒子 run，子 run
交出结论后再叫醒根 run。

★ engine 侧看到的只有 `SubAgentSpawner`（三个标量进、子 run id 出）。本模块是它唯一的
  实现，也是唯一知道「SubAgent 的会话是一行 thread」的地方。
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas_agent.domain.spec import AgentSpec, SubAgentSpec

from ..config import Settings
from ..db.models import Run, Thread
from ..executor.base import RunExecutor
from ..repositories.run import RunRepository
from ..repositories.thread import ThreadRepository
from ..stream.relay import EventRelay

logger = logging.getLogger(__name__)

__all__ = [
    "SubAgentRejected",
    "SubAgentService",
    "format_subagent_result",
    "subagent_result",
]

_TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


class SubAgentRejected(RuntimeError):
    """派不出去。消息是**给模型看的**，必须可操作。

    ★ 拒绝要在调用的那一刻发生，不能让模型等到锁超时才知道。撞上限、
      同轮同名、会话被占都归这里 —— kernel 的 SubAgentMiddleware 把它接成
      一条失败的工具结果，模型于是能改派、合并任务或自己动手。
    """


class SubAgentService:
    """`SubAgentSpawner` 的实现：一个 run（一段）派 SubAgent 的入口。

    ★ 生命周期绑定**一段执行**，不是进程级单例：每段建图时新建一个，绑定正在跑的
      run（即子 run 的父 run）。`_inflight` 记的是「这一批里已经在派谁」。
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
        #: 这一批里正在派的 persistent SubAgent 名 —— 同名并发在这里被拦住。
        self._inflight: set[str] = set()

    # ------------------------------------------------------------------ 入口

    async def spawn(self, name: str, task: str, *, fresh: bool = False) -> str:
        """`SubAgentSpawner` 的唯一实现：起一个子 run，立刻返回它的 id，不等它跑完。

        签名与 `atlas_engine.contracts.SubAgentSpawner` 保持一字不差 ——
        runtime_checkable 只查方法存在，参数形状靠 tests/test_seam_contracts.py
        的真实调用钉住。
        """
        sub = self._spec.subagent(name)
        if sub is None:
            known = ", ".join(s.name for s in self._spec.subagents) or "（无）"
            msg = f"SubAgent {name!r} 不存在。可用的是：{known}"
            raise SubAgentRejected(msg)

        # ★ 同轮同名在这里拒，而不是等它去撞会话串行锁。撞锁的表现是
        #   「卡住直到超时」，模型既不知道原因也拿不到替代方案。
        #
        #   只对持有会话的（persistent）SubAgent 成立：一次性 SubAgent 每次
        #   新建一个会话，彼此不共享状态也不争锁 —— 「并行调研五个选项」
        #   正是它存在的理由（设计 §09）。
        claimed = sub.session_mode == "persistent"
        if claimed:
            # ★ 检查与占位之间**一个 await 都不能有**。模型在一批里发两个 task
            #   是并发执行的：先 await 准入查询再占位的话，两者都能通过检查，
            #   于是同名的照样起了两个子 run —— 拦截形同虚设。
            if name in self._inflight:
                msg = (
                    f"{name} 已在这一批里被派出。它持有会话，同名的必须串行 —— "
                    f"把两个子任务合并成一次，或改派给不同的 SubAgent。"
                )
                raise SubAgentRejected(msg)
            self._inflight.add(name)

        try:
            async with self._sessionmaker() as session:
                parent = (
                    await session.execute(
                        select(
                            Run.created_by, Run.auth_uid, Run.root_run_id, Run.depth
                        ).where(Run.id == self._parent_run_id)
                    )
                ).one_or_none()
            # run 树的根：父 run 的根；父 run 自己是根（用户输入的那个 run）就是它
            root_run_id = (parent.root_run_id if parent else None) or self._parent_run_id
            await self._check_admission(root_run_id)
            run_id = await self._start(
                sub,
                task,
                fresh=fresh,
                root_run_id=root_run_id,
                depth=(parent.depth if parent else 0) + 1,
                created_by=parent.created_by if parent else None,
                auth_uid=parent.auth_uid if parent else None,
            )
            return str(run_id)
        finally:
            if claimed:
                self._inflight.discard(name)

    # ------------------------------------------------------------------ 准入

    async def _check_admission(self, root_run_id: UUID) -> None:
        async with self._sessionmaker() as session:
            runs = RunRepository(session)
            running = await runs.count_active_subruns()
            if running >= self._settings.subagent_max_running:
                msg = (
                    f"当前已有 {running} 个 SubAgent 在执行（上限 "
                    f"{self._settings.subagent_max_running}）。稍后重试，"
                    f"或先自己处理一部分。"
                )
                raise SubAgentRejected(msg)

            # ★ 按整棵树数（含 SubAgent 再派出去的）：层数不设上限，按父 run 数的话
            #   一条「A 派 A 再派 A……」的链永远撞不上限
            used = await runs.count_subruns_in_tree(root_run_id)
            if used >= self._settings.subagent_max_per_run:
                msg = (
                    f"这一轮已派出 {used} 个 SubAgent（含它们再派出去的），达到上限 "
                    f"{self._settings.subagent_max_per_run}。剩下的工作请自己完成，"
                    f"或在下一轮里继续。"
                )
                raise SubAgentRejected(msg)

    # ------------------------------------------------------------------ 起子 run

    async def _start(
        self,
        sub: SubAgentSpec,
        description: str,
        *,
        fresh: bool,
        root_run_id: UUID,
        depth: int,
        created_by: UUID | None,
        auth_uid: UUID | None,
    ) -> UUID:
        thread_id = await self._resolve_thread(sub, fresh=fresh)

        # 子会话也是 thread，会话串行锁原样适用 —— 同名 SubAgent 因此必然串行。
        # 上面的 _inflight 只是把这个必然的冲突提前到调用时暴露。
        run_id = uuid4()
        # 锁跟随租约（横向扩展设计 §4.4）：领到子 run 的执行器续它
        ttl = max(int(self._settings.run_lease_s), 1)
        # ★ 锁的 TTL 不再覆盖整个 run，串行的权威与主会话一样在 DB：还有没跑完的 run
        #   就不能再起一个（排队中没人续锁时，只靠 Redis 锁会放进第二个）
        async with self._sessionmaker() as session:
            busy = await RunRepository(session).active_run_of(thread_id) is not None
        if busy or not await self._relay.acquire_thread_lock(thread_id, owner=run_id, ttl_s=ttl):
            msg = (
                f"{sub.name} 的会话正被另一个 run 占用。它持有会话，同名的必须串行 —— "
                f"稍后重试，或改派给别的 SubAgent。"
            )
            raise SubAgentRejected(msg)

        try:
            async with self._sessionmaker() as session:
                await RunRepository(session).create(
                    run_id=run_id,
                    thread_id=thread_id,
                    agent_version_id=self._agent_version_id,
                    parent_run_id=self._parent_run_id,
                    root_run_id=root_run_id,
                    depth=depth,
                    # 子 run 的发起人 = 父 run 的发起人（权限设计 §11）；按谁判也沿用父 run
                    # （P2：子 Agent 自己的数字员工，内网身份传递 §6）
                    created_by=created_by,
                    auth_uid=auth_uid,
                )
                await session.commit()
        except Exception:
            await self._relay.release_thread_lock(thread_id, owner=run_id)
            raise

        # 起一条投递到子会话的用户消息：子会话的历史与父会话完全分开，
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
            "派出 SubAgent %s → 子会话 %s 子 run %s（父 run %s，深度 %d）",
            sub.name,
            thread_id,
            run_id,
            self._parent_run_id,
            depth,
        )
        return run_id

    # ------------------------------------------------------------------ 会话解析

    async def _resolve_thread(self, sub: SubAgentSpec, *, fresh: bool) -> UUID:
        """找到（或建出）这个 SubAgent 的会话。

        三条分支，差别只在「查不查已有子会话」：
          persistent + 未要求 fresh → 命中就复用，这就是「恢复」
          persistent + fresh        → 归档旧的、新建一个（历史不删）
          ephemeral                 → 永远新建，跑完归档 ⇒ 可以并行

        ★ 查找键的父会话是**派出它的那一层**：SubAgent 再派的 SubAgent 挂在它自己的
          会话下面，会话随 run 一起长成一棵树。
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
                #   一次性 SubAgent 可以真并行。subagent_name 照常写入，
                #   执行器要靠它派生 SubAgent 的 spec。
                status="ephemeral" if sub.session_mode == "ephemeral" else "active",
            )
            # ★ 技能按**子会话自己的** thread_id 投送。它挂的 workspace 是
            #   根会话的，skills 却必须是自己的（设计 §03）。
            if self._seed_skills is not None:
                await self._seed_skills(thread, sub)  # type: ignore[operator]
            await session.commit()
            return thread.id


#: 失败 / 被停止时附带的部分输出上限：它只是线索，不该比一次正常结论还长。
_PARTIAL_LIMIT = 4_000


async def subagent_result(session: AsyncSession, sub_run_id: UUID) -> str:
    """按子 run 的 id 取它的结论 —— 续跑时放进 decisions，由 SubAgentMiddleware 填进 `task` 的结果。

    ★ 兜底两种不该发生但必须有交代的情形：记录丢了、以及子 run 居然还没
      跑完。后者说明 join barrier 漏了一个口子；返回一句实话比让那个调用
      继续悬空强 —— 图会再挂起一次，而关卡已经判它结清，于是死循环。
    """
    runs = RunRepository(session)
    run = await runs.get(sub_run_id)
    if run is None:
        return "SubAgent 的 run 记录丢失，没有拿到结论。"

    thread = await ThreadRepository(session).by_id(run.thread_id)
    name = (thread.subagent_name if thread is not None else None) or "SubAgent"

    if run.status not in _TERMINAL:
        return f"{name} 尚未结束（状态 {run.status}），本轮拿不到它的结论。"

    text, tool_calls = await runs.run_output(run.id)
    started, finished = run.started_at, run.finished_at
    return format_subagent_result(
        name,
        status=run.status,
        error=run.error_message,
        text=text,
        tool_calls=tool_calls,
        tokens=int(run.total_tokens or 0),
        duration_s=(finished - started).total_seconds() if started and finished else None,
        persistent=thread is None or thread.status != "ephemeral",
    )


def format_subagent_result(
    name: str,
    *,
    status: str,
    error: str | None,
    text: str,
    tool_calls: int,
    tokens: int,
    duration_s: float | None,
    persistent: bool,
) -> str:
    """子 run 的结局 → 交给父模型的 ToolMessage 文本（纯函数，便于单测）。

    ★ 三种情形都不能让父模型读成「没有发现」：
        成功有结论    结论原文
        成功没结论    明确说没给结论、这不等于没发现、怎么补救
        失败 / 停止   照实说状态和原因；有部分输出就附上并标明「可能不完整」
    ★ 末尾一行统计：父模型据此判断这次花了多少、值不值得再派；持久
      会话还提示它下次只需补充新信息（与工具描述的说法一致）。
    """
    if status == "succeeded":
        body = text or (
            f"（{name} 完成了任务，但没有给出文字结论。这不等于「没有发现」："
            f"需要结论的话再派一次让它补上，或者自己核实。）"
        )
    else:
        detail = (error or "").strip()
        body = f"{name} 以 {status} 结束" + (f"：{detail}" if detail else "") + "。"
        if text:
            cut = len(text) > _PARTIAL_LIMIT
            clipped = text[:_PARTIAL_LIMIT] + ("\n…（已截断）" if cut else "")
            body += (
                "\n\n它停下之前的最后一段输出（部分结果，可能不完整，使用前请核实）：\n" + clipped
            )
        else:
            body += "\n\n它停下之前没有留下文字输出。"

    attrs = [
        f'subagent="{name}"',
        f'status="{status}"',
        f'session="{"persistent" if persistent else "ephemeral"}"',
        f'tokens="{tokens}"',
        f'tool_calls="{tool_calls}"',
    ]
    if duration_s is not None:
        attrs.append(f'duration_s="{round(duration_s)}"')
    return f"{body}\n\n<subagent {' '.join(attrs)}/>"
