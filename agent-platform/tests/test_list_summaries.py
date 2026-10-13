"""会话列表的 run 状态、智能体卡片的能力摘要（前端按原型改版所需的两处后端字段）。

★ 不用 clean_db（它会清掉开发库里的 agent）：写库的用例都在最后回滚的事务里做。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from atlas_server.db.models import Agent, AgentVersion, Run
from atlas_server.db.session import get_sessionmaker
from atlas_server.repositories.run import RunRepository
from atlas_server.services.agent import _summarize


async def test_latest_run_state_per_thread_in_rolled_back_tx() -> None:
    now = datetime.now(UTC)
    t_running, t_approval, t_sub_approval, t_done, t_none = (uuid.uuid4() for _ in range(5))
    version_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        try:

            def run(thread_id, status, *, at, waiting=None, parent=None):  # type: ignore[no-untyped-def]
                r = Run(
                    id=uuid.uuid4(),
                    thread_id=thread_id,
                    agent_version_id=version_id,
                    status=status,
                    waiting_on=waiting,
                    parent_run_id=parent,
                    created_at=at,
                )
                session.add(r)
                return r

            # 旧的失败 run 不影响：只看最近一个
            run(t_running, "failed", at=now - timedelta(minutes=5))
            run(t_running, "running", at=now)
            run(t_approval, "suspended", at=now, waiting=[{"reason": "approval", "token": "a"}])
            parent = run(
                t_sub_approval,
                "suspended",
                at=now,
                waiting=[{"reason": "delegation", "token": "x"}],
            )
            # 委派出去的子 run 在等审批：父会话也要算「需要我处理」
            run(
                uuid.uuid4(),
                "suspended",
                at=now,
                waiting=[{"reason": "approval"}],
                parent=parent.id,
            )
            run(t_done, "succeeded", at=now)
            await session.flush()

            states = await RunRepository(session).latest_runs_of(
                [t_running, t_approval, t_sub_approval, t_done, t_none]
            )
            assert states[t_running]["status"] == "running"
            assert states[t_running]["needs_approval"] is False
            assert states[t_approval]["needs_approval"] is True
            assert states[t_sub_approval]["waiting"] == ["delegation"]
            assert states[t_sub_approval]["needs_approval"] is True
            assert states[t_done]["status"] == "succeeded"
            assert t_none not in states
            assert await RunRepository(session).latest_runs_of([]) == {}
        finally:
            await session.rollback()


def test_agent_summary_exposes_capabilities() -> None:
    now = datetime.now(UTC)
    agent = Agent(
        id=uuid.uuid4(),
        slug="coord",
        name="协调者",
        description="",
        avatar_key="general",
        status="enabled",
        is_builtin=False,
        created_by=uuid.uuid4(),
        created_at=now,
        updated_at=now,
    )
    version = AgentVersion(
        id=uuid.uuid4(),
        agent_id=agent.id,
        version=7,
        created_by=uuid.uuid4(),
        spec={
            "model": {"model": "claude-sonnet-5"},
            "tool_names": [
                "filesystem",
                "task",
                "mcp:github:search",
                "mcp:github:get_pr",
                "mcp:serpapi:q",
            ],
            "skills": [{"slug": "a", "version": 1}, {"slug": "b", "version": 2}],
            "subagents": [
                {
                    "name": "claude-code",
                    "kind": "acp",
                    "cli": {"cli_type": "claude-code", "permission_mode": "auto"},
                },
                {"name": "searcher"},
            ],
        },
    )
    out = _summarize(agent, version)
    assert (out.kind, out.builtin_tool_count, out.skill_count) == ("native", 2, 2)
    assert out.mcp_servers == ["github", "serpapi"]
    assert out.subagents == [
        {"name": "claude-code", "kind": "acp", "permission_mode": "auto"},
        {"name": "searcher", "kind": "native", "permission_mode": None},
    ]
    # 历史快照没有这些字段也不出错
    bare = AgentVersion(
        id=uuid.uuid4(), agent_id=agent.id, version=1, created_by=uuid.uuid4(), spec={}
    )
    assert _summarize(agent, bare).kind == "native"
