"""thread_context 表：会话的上下文状态（原生运行时 · 上下文管理）。

见 c4-model/agent/code-native-runtime-context.html。

摘要和覆盖边界、压缩次数、自动压缩连续失败次数。Run 执行器重建历史时读它：摘要替换掉边界
之前的消息；压缩（自动或手动）写它。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ThreadContext
from ..db.session import fetch_one


class ThreadContextRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, thread_id: UUID) -> ThreadContext | None:
        """没压缩过、也没失败过的会话没有这一行。"""
        return await fetch_one(self._session, ThreadContext, ThreadContext.thread_uuid == thread_id)

    async def compact_counts(self, thread_ids: list[UUID]) -> dict[UUID, int]:
        """会话列表一次取齐压缩次数（没有行的就是 0）。"""
        if not thread_ids:
            return {}
        rows = await self._session.execute(
            select(ThreadContext.thread_uuid, ThreadContext.compact_count).where(
                ThreadContext.thread_uuid.in_(thread_ids)
            )
        )
        return {thread_id: count for thread_id, count in rows.all()}

    async def save_summary(self, thread_id: UUID, *, summary: str, upto: datetime) -> None:
        """压缩成功：摘要覆盖到 upto，次数加一，熔断计数清零。"""
        row = await self._row(thread_id)
        row.summary, row.summary_upto = summary, upto
        row.compact_count = (row.compact_count or 0) + 1
        row.failure_count = 0

    async def record_failure(self, thread_id: UUID, failures: int) -> None:
        """自动压缩连续失败了几次（熔断用）。"""
        row = await self._row(thread_id)
        row.failure_count = failures

    async def _row(self, thread_id: UUID) -> ThreadContext:
        row = await self.get(thread_id)
        if row is None:
            row = ThreadContext(thread_uuid=thread_id, compact_count=0, failure_count=0)
            self._session.add(row)
        return row
