"""向用户提问的 server 侧链路（doc/ask-user-question-design.html）：回答 → 续跑 → 模型拿到答案。

engine 侧的挂起与结果文本见 test_ask_user.py。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from atlas_agent.config import get_settings
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.executor.inprocess import InProcessExecutor
from atlas_agent.main import create_app
from atlas_engine.kernel.middleware.ask_user import ASK_USER_QUESTION
from httpx import ASGITransport
from langchain_core.messages import AIMessageChunk, ToolMessage

from tests.fake_uc import AS_USER
from tests.fakes import TurnModel, tool_call_chunk
from tests.test_runs_api import wait_for_status

QUESTIONS = [
    {
        "question": "你要做哪类工作流？",
        "header": "业务场景",
        "options": [
            {"label": "审批流", "description": "最常见", "recommended": True},
            {"label": "工单流转", "description": "受理、派单、处理"},
        ],
    }
]
SPEC = {
    "system_prompt": "p",
    "model": {"model": "claude-opus-5"},
    "tool_names": [ASK_USER_QUESTION],
}


def make_app():
    # ★ 同一个模型实例跨段复用：续跑段接着脚本往下走，而不是从头再问一遍
    model = TurnModel(
        scripts=[
            [tool_call_chunk(ASK_USER_QUESTION, json.dumps({"questions": QUESTIONS}), "c1")],
            [AIMessageChunk(content="需求摘要：……")],
        ]
    )
    app = create_app()
    app.state.executor = InProcessExecutor(
        get_sessionmaker(), get_settings(), model_builder=lambda *_a, **_k: model
    )
    app.state.test_model = model
    return app


@pytest.fixture
async def app():
    return make_app()


@pytest.fixture
async def client(app) -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=AS_USER
    ) as c:
        yield c


async def _start(client: httpx.AsyncClient) -> tuple[str, dict]:
    """发起一个会提问的 run，返回 (run_id, 待回答的问题)。"""
    agent = await client.post("/v1/agents", json={"slug": "asker", "name": "提问", "spec": SPEC})
    assert agent.status_code == 201, agent.text
    thread = await client.post("/v1/threads", json={"agent_id": agent.json()["id"]})
    res = await client.post(
        f"/v1/threads/{thread.json()['id']}/runs",
        json={"content": [{"type": "text", "text": "做个工作流的原型"}]},
    )
    run_id = res.json()["run_id"]
    for _ in range(60):
        pending = (await client.get(f"/v1/runs/{run_id}/approvals")).json()["data"]
        if pending:
            return run_id, pending[0]
        await asyncio.sleep(0.1)
    raise AssertionError("没等到待回答的问题")


def _answer_seen_by_model(app) -> str:
    """续跑段模型看到的提问结果。

    ★ 按 tool_call_id 找，不取「最后一次调用」—— 标题生成也调这个模型。
    """
    results = [
        m
        for call in app.state.test_model.seen
        for m in call
        if isinstance(m, ToolMessage) and m.tool_call_id == "c1"
    ]
    assert results, "模型没有看到提问的结果"
    return str(results[-1].content)


@pytest.mark.usefixtures("clean_db")
async def test_an_answer_resumes_the_run_and_reaches_the_model(
    client: httpx.AsyncClient, app
) -> None:
    """★ 整条链路：问题落成 kind=question 的待决项 → 提交答案 → 同一轮继续，模型拿到答案。"""
    run_id, pending = await _start(client)
    assert pending["kind"] == "question"
    assert pending["args"]["questions"][0]["header"] == "业务场景"
    run = (await client.get(f"/v1/runs/{run_id}")).json()
    assert run["status"] == "awaiting_approval"

    res = await client.post(
        f"/v1/runs/{run_id}/approvals/{pending['id']}",
        json={"decision": "approved", "answers": [{"selected": ["工单流转"]}]},
    )
    assert res.status_code == 200, res.text
    assert (await wait_for_status(client, run_id))["status"] == "succeeded"
    answer = _answer_seen_by_model(app).splitlines()[1]
    assert answer == "1. 业务场景（你要做哪类工作流？）→ 工单流转"


@pytest.mark.usefixtures("clean_db")
async def test_answers_that_do_not_fit_are_refused(client: httpx.AsyncClient) -> None:
    """答案和问题对不上：400，问题仍在等（没有被错误地消费）。"""
    run_id, pending = await _start(client)
    url = f"/v1/runs/{run_id}/approvals/{pending['id']}"
    body = {"decision": "approved", "answers": [{"selected": ["BPMN"]}]}
    bad = await client.post(url, json=body)
    assert bad.status_code == 400, bad.text
    assert bad.json()["error"]["kind"] == "invalid_answers"
    assert (await client.get(f"/v1/runs/{run_id}/approvals")).json()["data"]


@pytest.mark.usefixtures("clean_db")
async def test_a_blind_approval_takes_the_recommendations(
    client: httpx.AsyncClient, app
) -> None:
    """不认识问题的调用方（bench、开放接口）照常「批准」：按推荐继续，不卡住（设计 D8）。"""
    run_id, pending = await _start(client)
    res = await client.post(
        f"/v1/runs/{run_id}/approvals/{pending['id']}", json={"decision": "approved"}
    )
    assert res.status_code == 200, res.text
    assert (await wait_for_status(client, run_id))["status"] == "succeeded"
    assert _answer_seen_by_model(app).splitlines()[1] == (
        "1. 业务场景（你要做哪类工作流？）→ 审批流（用户没有逐题作答，按你的推荐）"
    )
