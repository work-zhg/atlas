"""cli_session：CLI 会话的接续信息按会话一行（迁移 0031，code-cli-runtime-link.html）。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from atlas_agent.db.models import CliSession
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.repositories.cli_session import CliSessionRepository
from sqlalchemy import delete, func, select


@pytest.fixture
async def thread_id():
    tid = uuid4()
    yield tid
    async with get_sessionmaker()() as session:
        await session.execute(delete(CliSession).where(CliSession.thread_uuid == tid))
        await session.commit()


async def test_session_id_and_progress_share_one_row_per_thread(thread_id) -> None:
    """★ 会话 id 和确认点都是会话级的：跨 run、跨实例都读同一行，接管的实例据此 attach。"""
    async with get_sessionmaker()() as session:
        await CliSessionRepository(session).remember(thread_id, "agent-session-1")
        await session.commit()
    async with get_sessionmaker()() as session:
        await CliSessionRepository(session).save_progress(thread_id, instance="b-1", acked_seq=7)
        await session.commit()
    async with get_sessionmaker()() as session:
        await CliSessionRepository(session).save_progress(thread_id, instance="b-1", acked_seq=12)
        await session.commit()

    async with get_sessionmaker()() as session:
        row = await CliSessionRepository(session).get(thread_id)
        count = await session.scalar(
            select(func.count()).select_from(CliSession).where(CliSession.thread_uuid == thread_id)
        )
    assert count == 1
    assert row is not None
    assert (row.agent_session_id, row.bridge_instance, row.acked_seq) == (
        "agent-session-1",
        "b-1",
        12,
    )
