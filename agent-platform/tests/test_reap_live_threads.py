"""沙箱回收的「活会话」口径（ThreadRepository.live_ids，维护者的孤儿回收用）。

★ 归档的会话不算活：task 的 fresh=true 丢弃的子会话不会再被复用，它的 ACP Pod 不回收就一直占着
  配额（2026-10-20 服务器上试跑时一轮留下了 3 个）。但归档了还有 run 没跑完的要算活 —— 回收它的
  沙箱等于把正在跑的那一轮掐掉。
"""

from __future__ import annotations

import pytest
from atlas_agent.db.models import Agent, AgentVersion
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.repositories.run import RunRepository
from atlas_agent.repositories.thread import ThreadRepository

pytestmark = pytest.mark.usefixtures("clean_db")

USER = "00000000-0000-0000-0000-000000000001"


async def test_archived_threads_are_not_live_unless_a_run_is_unfinished() -> None:
    async with get_sessionmaker()() as s:
        threads = ThreadRepository(s)
        runs = RunRepository(s)
        agent = Agent(slug="reap", name="回收", status="enabled", created_by=USER)
        s.add(agent)
        await s.flush()
        ver = AgentVersion(agent_id=agent.id, version=1, spec={}, created_by=USER)
        s.add(ver)
        await s.flush()
        version = ver.id
        aid = agent.id
        active = await threads.create(agent_id=aid, title="在用", created_by=USER)
        dropped = await threads.create(agent_id=aid, title="fresh 丢弃的", created_by=USER)
        busy = await threads.create(agent_id=aid, title="归档时还在跑", created_by=USER)
        done = await threads.create(agent_id=aid, title="归档且跑完了", created_by=USER)
        for t in (dropped, busy, done):
            await threads.archive_subagent(t)
        await runs.create(thread_id=busy.id, agent_version_id=version)  # 新建的 run 是 queued
        finished = await runs.create(thread_id=done.id, agent_version_id=version)
        finished.status = "succeeded"
        await s.commit()

        live = set(await threads.live_ids())
    assert active.id in live
    assert busy.id in live
    assert dropped.id not in live
    assert done.id not in live
