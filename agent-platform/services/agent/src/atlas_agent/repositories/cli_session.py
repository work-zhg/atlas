"""cli_session 表：CLI 会话的接续信息（CLI 运行时 · 上游连接，code-cli-runtime-link.html）。

一个会话一行，每次确认时保存；接管的实例从这里读，attach 回 bridge、从确认点之后补发。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import CliSession
from ..db.session import fetch_one


class CliSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, thread_id: UUID) -> CliSession | None:
        return await fetch_one(self._session, CliSession, CliSession.thread_uuid == thread_id)

    async def remember(self, thread_id: UUID, agent_session_id: str) -> None:
        """CLI 自己的会话 id：下一次 open 时带上它，CLI 从持久化的 HOME 里恢复对话。"""
        row = await self._row(thread_id)
        row.agent_session_id = agent_session_id[:128]

    async def save_progress(self, thread_id: UUID, *, instance: str, acked_seq: int) -> None:
        """已处理到的 bridge 实例与序号。换实例接管时据此 attach，从这里之后补发。"""
        row = await self._row(thread_id)
        row.bridge_instance, row.acked_seq = instance[:64], acked_seq

    async def _row(self, thread_id: UUID) -> CliSession:
        # ★ 同一会话的 run 被会话锁串行化，不会有两个写入方同时建这一行
        row = await self.get(thread_id)
        if row is None:
            row = CliSession(thread_uuid=thread_id)
            self._session.add(row)
        return row
