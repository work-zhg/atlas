from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from atlas_server.domain.events import TraceEvent
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AgentVersion, Message, Run, RunEvent, Thread

_TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")


class RunRepository:
    """唯一接触 run / run_event 表的地方。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ 读

    async def get(self, run_id: UUID) -> Run | None:
        return await self._session.get(Run, run_id)

    async def load_for_execution(
        self, run_id: UUID
    ) -> tuple[Run, Thread, AgentVersion] | None:
        """取执行一次 run 所需的三行。

        试跑移除后 thread_id / agent_version_id 均非空（迁移 0006），
        内连接即可 —— 查不到就是 run 真的不存在。
        """
        stmt = (
            select(Run, Thread, AgentVersion)
            .join(AgentVersion, AgentVersion.id == Run.agent_version_id)
            .join(Thread, Thread.id == Run.thread_id)
            .where(Run.id == run_id)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        return (row[0], row[1], row[2]) if row else None

    async def history(
        self,
        thread_id: UUID,
        *,
        after: datetime | None = None,
        limit: int = 2_000,
    ) -> list[Message]:
        """按时间正序取对话历史。

        `after` 是摘要覆盖的边界（thread.summary_upto）：早于它的消息已经
        被压进摘要，不再重复送给模型（§7.4）。

        ★ limit 从 100 提到 2000 并**只作为安全网**。原先的 100 是一道
          无条件硬截断，且发生在压缩之前 —— 超过 100 条的会话，更早的消息
          直接消失，既不进模型也不产生摘要，用户感受是「它忘了」而不是
          「已压缩」。收缩上下文的职责现在唯一地归压缩。
        """
        stmt = select(Message).where(Message.thread_id == thread_id)
        if after is not None:
            stmt = stmt.where(Message.created_at > after)
        stmt = stmt.order_by(Message.created_at.desc()).limit(limit)
        rows = list((await self._session.execute(stmt)).scalars())
        return list(reversed(rows))

    async def archived_events(self, run_id: UUID, *, after_seq: int) -> list[RunEvent]:
        stmt = (
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
            .order_by(RunEvent.seq)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def active_runs(self) -> list[tuple[UUID, UUID]]:
        """启动时扫描：进程内执行的 run 在重启后成了孤儿（文档 §12.1 / 风险 R2）。

        连 thread_id 一起返回 —— 回收孤儿时要顺手释放它持有的会话串行锁。
        """
        stmt = select(Run.id, Run.thread_id).where(
            Run.status.in_(["queued", "running", "awaiting_approval"])
        )
        return [(row[0], row[1]) for row in (await self._session.execute(stmt)).all()]

    async def active_run_of(self, thread_id: UUID) -> UUID | None:
        """该会话当前**还没跑完**的 run —— 刷新页面后恢复事件流要用它。

        ★ 没有它的话，刷新 = 白屏：前端的 activeRunId 只在「发消息成功」时
          赋值，重新挂载后是 undefined，于是不订阅任何流；而助手消息要到
          message.completed 才落库，长 run 期间 message 表里只有用户那条。
          用户看到的就是「回答到一半没了，刷新后什么都没有」。

        ★ 状态口径与 active_runs 一致（含 awaiting_approval）：等审批的 run
          在用户眼里正是「还在跑」，恢复出来才能看见弹窗。
        """
        stmt = (
            select(Run.id)
            .where(
                Run.thread_id == thread_id,
                Run.status.in_(["queued", "running", "awaiting_approval"]),
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().one_or_none()

    async def last_assistant_content(self, thread_id: UUID) -> list | None:
        """子会话最后一条 assistant 消息的内容 —— 委派的返回值。

        ★ 取自 message 表而不是事件流：message 是事实源，而事件在 Redis 里
          有 TTL。子 run 跑完之后父才来读，中间可能隔着审批等待。
        """
        stmt = (
            select(Message.content)
            .where(Message.thread_id == thread_id, Message.role == "assistant")
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().one_or_none()

    async def count_active_subruns(self) -> int:
        """全进程正在执行的子 run 数 —— 准入控制的第一道闸。

        ★ 必须是全局的：acp 子智能体各吃一个 Pod，按 run 各自限流的话
          10 个并发会话每个委派 2 个 = 20 个 Pod。
        """
        stmt = (
            select(func.count())
            .select_from(Run)
            .where(
                Run.parent_run_id.isnot(None),
                Run.status.in_(["queued", "running", "awaiting_approval"]),
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def count_subruns_of(self, run_id: UUID) -> int:
        """某个父 run 累计发起过几次委派 —— 准入控制的第二道闸。

        防的是一种绕过：长 run 在每个规划点发起一批「合法尺寸」的委派，
        累计起来远超并发限制。查 run 表即可，不用单独的委派账本。
        """
        stmt = select(func.count()).select_from(Run).where(Run.parent_run_id == run_id)
        return int((await self._session.execute(stmt)).scalar_one())

    # ------------------------------------------------------------------ 写

    async def create(
        self,
        *,
        thread_id: UUID,
        agent_version_id: UUID,
        run_id: UUID | None = None,
        parent_run_id: UUID | None = None,
    ) -> Run:
        """run_id 可由调用方预生成：会话串行锁的 owner 值是 run_id，而锁必须
        先于 run 行拿到（拿不到就不该插行）—— 见 RunService.create。

        parent_run_id 非空即「这是一次委派产生的子 run」。
        """
        run = Run(
            thread_id=thread_id,
            agent_version_id=agent_version_id,
            parent_run_id=parent_run_id,
            status="queued",
        )
        if run_id is not None:
            run.id = run_id
        self._session.add(run)
        await self._session.flush()
        await self._session.refresh(run)
        return run

    async def set_status(self, run_id: UUID, status: str) -> None:
        """仅用于运行中的状态切换（running ⇄ awaiting_approval）。

        ★ 不碰终止态：run 一旦 succeeded/failed/cancelled，事件集就是不可变的
          （§10.2 的回放依赖这一点），把它改回 running 会让已关闭的 SSE
          再也无法正确回放。
        """
        await self._session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status.notin_(list(_TERMINAL)))
            .values(status=status)
        )

    async def mark_running(self, run_id: UUID) -> None:
        await self._session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(status="running", started_at=datetime.now(UTC))
        )

    async def mark_interrupted(self, run_ids: list[UUID]) -> int:
        if not run_ids:
            return 0
        result = await self._session.execute(
            update(Run)
            .where(Run.id.in_(run_ids))
            .values(
                status="interrupted",
                error_kind="interrupted",
                error_message="服务重启导致中断，可重试",
                finished_at=datetime.now(UTC),
            )
        )
        return int(result.rowcount or 0)

    async def finish(
        self,
        run_id: UUID,
        *,
        status: str,
        last_seq: int,
        usage: dict[str, int],
        error_kind: str | None = None,
        error_message: str | None = None,
    ) -> None:
        await self._session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                status=status,
                last_seq=last_seq,
                error_kind=error_kind,
                error_message=error_message,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read", 0),
                thinking_tokens=usage.get("thinking_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                finished_at=datetime.now(UTC),
            )
        )

    async def archive_events(self, events: list[TraceEvent]) -> None:
        """run 结束时批量落库 —— Redis 是实时通道，这里是历史（文档 §10.2）。"""
        if not events:
            return
        await self._session.execute(
            insert(RunEvent),
            [
                {
                    "run_id": e.run_id,
                    "seq": e.seq,
                    "ts": e.ts,
                    "type": e.type.value,
                    "depth": e.depth,
                    "data": e.data,
                }
                for e in events
            ],
        )

    async def touch_thread(self, thread_id: UUID, *, latest_state: dict | None = None) -> None:
        values: dict = {
            "updated_at": func.now(),
            "message_count": (
                select(func.count())
                .select_from(Message)
                .where(Message.thread_id == thread_id)
                .scalar_subquery()
            ),
        }
        if latest_state is not None:
            values["latest_state"] = latest_state
        await self._session.execute(update(Thread).where(Thread.id == thread_id).values(**values))
