from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Approval


class ApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, approval_id: UUID, run_id: UUID, tool_name: str, args: dict[str, Any]
    ) -> Approval:
        """id 由 engine 生成并一同发进 approval.required 事件 ——
        前端拿到事件就能直接构造决策 URL，不必再查一次。"""
        row = Approval(
            id=approval_id, run_id=run_id, tool_name=tool_name, args=args, status="pending"
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, approval_id: UUID) -> Approval | None:
        stmt = select(Approval).where(Approval.id == approval_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_pending(self, run_id: UUID) -> list[Approval]:
        stmt = (
            select(Approval)
            .where(Approval.run_id == run_id, Approval.status == "pending")
            .order_by(Approval.created_at)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def decide(self, approval_id: UUID, *, decision: str, user_id: UUID) -> bool:
        """★ WHERE status='pending' 保证只有第一次决策生效。

        多标签页、重复点击、以及「用户点批准的同时后端刚好判定超时」都会
        产生并发决策；先查后写有窗口，条件写进 UPDATE 才安全。
        """
        stmt = (
            update(Approval)
            .where(Approval.id == approval_id, Approval.status == "pending")
            .values(status=decision, decided_by=user_id, decided_at=datetime.now(UTC))
        )
        return bool((await self._session.execute(stmt)).rowcount)

    async def expire(self, approval_id: UUID) -> bool:
        stmt = (
            update(Approval)
            .where(Approval.id == approval_id, Approval.status == "pending")
            .values(status="expired", decided_at=datetime.now(UTC))
        )
        return bool((await self._session.execute(stmt)).rowcount)
