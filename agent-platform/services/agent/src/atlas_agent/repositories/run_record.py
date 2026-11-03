"""run 表上与执行有关的写（Run 执行器 · 一段的执行与落库，code-executor-segment.html）。

载入、标记运行中、检查点推进序号、挂起、收尾，以及跨段 state（run_state）。带 owner 的写都是
租约围栏：租约已不在本实例就抛 LeaseLost，整个事务回滚。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_agent.domain.messages import KIND_CHAT

from ..db.models import AgentVersion, Message, Run, RunState, Thread
from .run import (
    _LEASED,
    LeaseLost,
    _held_by,
    _usage_increments,
)


class RunRecordRepository:
    """一个 run 的执行记录：带租约围栏的写，和跨段 state。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_for_execution(self, run_id: UUID) -> tuple[Run, Thread, AgentVersion] | None:
        """取执行一次 run 所需的三行。

        试跑移除后 thread_id / agent_version_id 均非空（迁移 0006），
        内连接即可 —— 查不到就是 run 真的不存在。
        """
        stmt = (
            select(Run, Thread, AgentVersion)
            .join(AgentVersion, AgentVersion.agent_version_uuid == Run.agent_version_uuid)
            .join(Thread, Thread.thread_uuid == Run.thread_uuid)
            .where(Run.run_uuid == run_id)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        return (row[0], row[1], row[2]) if row else None

    async def mark_running(self, run_id: UUID) -> None:
        """★ started_at 只在第一次设。

        一个 run 可以分多段执行，每段都会走到这里。每次都重设的话，
        「这一轮跑了多久」会变成「最后一段跑了多久」—— 而委派挂起的那一
        小时恰恰是最该被看见的部分。COALESCE 让它保持第一段的时间。
        """
        await self._session.execute(
            update(Run)
            .where(Run.run_uuid == run_id)
            .values(
                status="running",
                started_at=func.coalesce(Run.started_at, datetime.now(UTC)),
            )
        )

    async def finish(
        self,
        run_id: UUID,
        *,
        status: str,
        last_seq: int,
        usage: dict[str, int],
        error_kind: str | None = None,
        error_message: str | None = None,
        owner: str | None = None,
    ) -> None:
        """owner: 执行器收尾时传本实例 id —— 租约已不在本实例就抛 LeaseLost（围栏）。
        不传 = 没有执行器在跑的收尾（挂起中被取消、兜底失败）。"""
        result = await self._session.execute(
            update(Run)
            .where(Run.run_uuid == run_id, *_held_by(owner))
            .values(
                status=status,
                last_seq=last_seq,
                error_kind=error_kind,
                error_message=error_message,
                **_usage_increments(usage),
                finished_at=datetime.now(UTC),
                lease_until=None,
                # 挂起现场随 run 结束作废（续跑段要读它，所以留到这里才清，见 claim_for_resume）
                waiting_on=None,
                awaiting_user=False,
            )
        )
        if owner is not None and not result.rowcount:
            raise LeaseLost(run_id)

    async def suspend(
        self,
        run_id: UUID,
        *,
        last_seq: int,
        usage: dict[str, int],
        waiting_on: dict[str, dict[str, str]] | None = None,
        owner: str | None = None,
    ) -> None:
        """一段跑完了，但这个 run 还没结束 —— 等子 run 回来再续。

        ★ 用量与 seq 必须当场落下。下一段要从 `last_seq` 接着数事件序号
          （契约规则 2 的「无空洞」跨段成立），也要从 `total_tokens` 接着
          算 token 上限 —— 每段各算各的话，一个 run 分三段就能烧掉三倍的
          预算，而「刹车」在账面上看起来一直没踩。

        ★ 不写 finished_at：它没结束。

        ★ waiting_on 记「在等什么」：{tool_call_id: {reason, token}}，与图的 state 同形
          （contracts/suspension.py）。续跑的 barrier 按 reason 分派 —— 委派等子 run
          终态，审批等 approval 被决策；续跑时它与消息一起交回图。
        """
        result = await self._session.execute(
            update(Run)
            .where(Run.run_uuid == run_id, *_held_by(owner))
            .values(
                # ★ 等人还是等子 run 都是 suspended：在等什么看 waiting_on 的原因
                status="suspended",
                last_seq=last_seq,
                waiting_on=waiting_on or None,
                **_usage_increments(usage),
                # 挂起 = 没有进程在跑：租约一并清掉，它不该被续租，也不该被当成过期接管
                owner=None,
                lease_until=None,
            )
        )
        if owner is not None and not result.rowcount:
            raise LeaseLost(run_id)

    async def set_awaiting_user(self, run_id: UUID, *, waiting: bool) -> None:
        """CLI 在线等权限：run 仍是 running（CLI 和 bridge 都在等），界面据此显示「需要你处理」。"""
        await self._session.execute(
            update(Run).where(Run.run_uuid == run_id).values(awaiting_user=waiting)
        )

    async def checkpoint(self, run_id: UUID, *, owner: str, last_seq: int) -> None:
        """检查点的围栏：推进 last_seq，同时确认租约仍在本实例。不在就抛 LeaseLost。

        ★ 必须是检查点事务里的**第一条**写：它拿到 run 行锁，与接管者的领取互斥。
        """
        result = await self._session.execute(
            update(Run)
            .where(Run.run_uuid == run_id, Run.owner == owner, Run.status.in_(_LEASED))
            .values(last_seq=func.greatest(Run.last_seq, last_seq))
        )
        if not result.rowcount:
            raise LeaseLost(run_id)

    async def save_acp_progress(self, run_id: UUID, *, instance: str, acked_seq: int) -> None:
        """acp：已处理到的 bridge 实例与序号（§5.2）。换实例接管时据此 attach。"""
        await self._session.execute(
            update(Run)
            .where(Run.run_uuid == run_id)
            .values(acp_bridge_instance=instance[:64], acp_acked_seq=acked_seq)
        )

    async def load_state(self, run_id: UUID) -> dict[str, Any]:
        """这个 run 落过库的图 state（run_state 表），续跑与接管时交回图。全新的 run 返回 {}。"""
        rows = await self._session.execute(
            select(RunState.key, RunState.value).where(RunState.run_uuid == run_id)
        )
        return {key: value for key, value in rows.all()}

    async def save_state(self, run_id: UUID, values: Mapping[str, Any]) -> None:
        """逐键 upsert：只写传进来的键，别的键不动。

        ★ 必须与检查点（或段收尾）在同一个事务里：接管者从最后一个检查点接着跑，
          state 要与那时落库的消息对得上。
        """
        if not values:
            return
        table = RunState.__table__
        rows = [{"run_uuid": run_id, "key": key, "value": value} for key, value in values.items()]
        dialect = self._session.bind.dialect.name if self._session.bind else "postgresql"
        if dialect in ("mysql", "mariadb"):
            from sqlalchemy.dialects.mysql import insert as mysql_insert

            stmt: Any = mysql_insert(table).values(rows)
            stmt = stmt.on_duplicate_key_update(value=stmt.inserted.value, updated_at=func.now())
        else:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(table).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["run_uuid", "key"],
                set_={"value": stmt.excluded.value, "updated_at": func.now()},
            )
        await self._session.execute(stmt)

    async def touch_thread(self, thread_id: UUID, *, latest_state: dict | None = None) -> None:
        values: dict = {
            "updated_at": func.now(),
            # ★ 只数对话轮，与 ThreadRepository.list_messages / count_messages
            #   同口径。工具结果也数进去的话，一轮调十次工具就凭空多二十条，
            #   而用户只发了一句话。
            "message_count": (
                select(func.count())
                .select_from(Message)
                .where(Message.thread_uuid == thread_id, Message.kind == KIND_CHAT)
                .scalar_subquery()
            ),
        }
        if latest_state is not None:
            values["latest_state"] = latest_state
        await self._session.execute(
            update(Thread).where(Thread.thread_uuid == thread_id).values(**values)
        )
