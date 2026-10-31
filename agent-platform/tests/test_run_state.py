"""run_state：图 state 里 message 表装不下的部分（todos），跨段带回（迁移 0028）。

Atlas 不用 checkpointer，每段从 message 表重建 state。messages 以外的字段不落库的话，
挂起续跑之后就是空的 —— 用户看不出来，但压缩中间件的待办提醒（读 state.todos）从此不再出现。

两层：
  · runner：交回的 todos 进了图，续跑段里待办提醒照常生效（不连库）
  · 执行器：第一段写下的 todos 落进 run_state，续跑段读出来交给 runner；删会话时一并删掉
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from atlas_agent.config import get_settings
from atlas_agent.db.models import RunState
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.executor import runtime
from atlas_agent.executor.inprocess import InProcessExecutor
from atlas_agent.main import create_app
from atlas_engine.kernel.middleware.compaction import CompactionMiddleware
from httpx import ASGITransport
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from sqlalchemy import select

from tests.fake_uc import AS_USER
from tests.test_runs_api import wait_for_status

from .fakes import TurnModel, tool_call_chunk
from .graphs import run_agent

TODOS = [
    {"content": "查资料", "status": "completed"},
    {"content": "写报告", "status": "in_progress"},
]


# ──────────────────────────────────────────────── runner：交回的 todos 进了图


def _spec() -> object:
    from atlas_agent.domain.spec import AgentSpec, LimitSpec, ModelSpec

    return AgentSpec(
        slug="todo",
        name="待办",
        system_prompt="p",
        model=ModelSpec(model="claude-sonnet-5"),
        tool_names=("write_todos",),
        limits=LimitSpec(timeout_s=30),
    )


async def _summ(messages: Any) -> str:
    return "摘要"


@pytest.mark.parametrize(("run_state", "reminded"), [(None, False), ({"todos": TODOS}, True)])
async def test_the_todo_reminder_fires_in_a_resumed_segment_only_with_run_state(
    run_state: dict[str, Any] | None, reminded: bool
) -> None:
    """续跑段：历史里有上一段的 write_todos，本段从工具结果之后接着调模型。

    不交回 run_state 时 state.todos 为空，提醒不出现 —— 这就是修之前的样子。
    阈值设成 0：只看「state 里有没有计划」，不看隔了几步。
    """
    history = [
        HumanMessage(content="写份报告"),
        AIMessage(
            content="",
            tool_calls=[{"id": "c0", "name": "write_todos", "args": {"todos": TODOS}}],
        ),
        ToolMessage(content="Updated todo list", tool_call_id="c0"),
    ]
    model = TurnModel(scripts=[[AIMessageChunk(content="继续写")]])
    compactor = CompactionMiddleware(
        context_window=200_000,
        trigger_ratio=0.8,
        keep_recent_turns=2,
        summarizer=_summ,
        todo_reminder_after=0,
    )

    async for _ in run_agent(
        _spec(),
        model,
        run_id=uuid4(),
        input_content="",
        history=history,
        resume=True,
        compactor=compactor,
        run_state=run_state,
    ):
        pass

    last = model.seen[0][-1]
    got = isinstance(last, HumanMessage) and "<system-reminder" in str(last.content)
    assert got is reminded, last
    if reminded:
        assert "写报告" in str(last.content)


# ──────────────────────────────────────────────── 执行器：落库、续跑读回、随会话删除

#: 规划模式：先记待办，再提交计划（exit_plan_mode）→ 审批挂起 → 批准后续跑
SPEC = {
    "system_prompt": "p",
    "model": {"model": "claude-opus-5"},
    "tool_names": ["write_todos"],
    "approval": {"default_mode": "plan"},
}


@pytest.fixture
async def client_and_states(monkeypatch: pytest.MonkeyPatch):
    """(client, 每段交给 runner 的 run_state)。

    ★ 模型是**同一个实例**：每段重新装配图时都拿到它，脚本跨段接着往下走
      （每段新建一个的话续跑段会从第一段的脚本重来）。
    """
    todos_args = json.dumps({"todos": TODOS}, ensure_ascii=False)
    model = TurnModel(
        scripts=[
            [tool_call_chunk("write_todos", todos_args, "c0")],
            [tool_call_chunk("exit_plan_mode", '{"plan":"先查资料再写报告"}', "c1")],
            [AIMessageChunk(content="办完了")],
        ]
    )
    states: list[dict[str, Any]] = []
    real = runtime.engine_run

    def spy(*args: Any, **kwargs: Any) -> Any:
        states.append(dict(kwargs.get("run_state") or {}))
        return real(*args, **kwargs)

    monkeypatch.setattr(runtime, "engine_run", spy)

    app = create_app()
    app.state.executor = InProcessExecutor(
        get_sessionmaker(), get_settings(), model_builder=lambda *_a, **_k: model
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AS_USER) as c:
        yield c, states


async def _stored(run_id: str) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(RunState.key, RunState.value).where(RunState.run_id == UUID(run_id))
        )
        return dict(rows.all())


@pytest.mark.usefixtures("clean_db")
async def test_todos_are_stored_and_handed_back_to_the_resumed_segment(client_and_states) -> None:
    client, states = client_and_states
    agent = await client.post("/v1/agents", json={"slug": "planner", "name": "规划", "spec": SPEC})
    assert agent.status_code == 201, agent.text
    thread = await client.post("/v1/threads", json={"agent_id": agent.json()["id"], "title": "t"})
    thread_id = thread.json()["id"]
    res = await client.post(
        f"/v1/threads/{thread_id}/runs", json={"content": [{"type": "text", "text": "写份报告"}]}
    )
    run_id = res.json()["run_id"]

    approval_id = None
    for _ in range(60):
        pending = (await client.get(f"/v1/runs/{run_id}/approvals")).json()["data"]
        if pending:
            approval_id = pending[0]["id"]
            break
        await asyncio.sleep(0.1)
    assert approval_id is not None, "没等到计划审批"

    # ① 挂起时，第一段写下的待办已经落库
    assert await _stored(run_id) == {"todos": TODOS}

    res = await client.post(
        f"/v1/runs/{run_id}/approvals/{approval_id}",
        json={"decision": "approved", "approval_mode": "auto"},
    )
    assert res.status_code == 200, res.text
    run = await wait_for_status(client, run_id)
    assert run["status"] == "succeeded", run

    # ② 首段从空开始；续跑段拿到的是上一段落库的待办
    assert states == [{}, {"todos": TODOS}]

    # ③ 删会话时一并删掉（没有外键，靠 ThreadRepository.delete）
    assert (await client.delete(f"/v1/threads/{thread_id}")).status_code == 204
    assert await _stored(run_id) == {}
