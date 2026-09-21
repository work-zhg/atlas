"""P7 · 人工确认的 server 侧链路（文档 §12.2）。

engine 侧的时序与决策语义见 test_approval.py。
这里测的是「HTTP 决策能不能唤醒正在阻塞的执行器」—— 整条链路的接缝。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import redis.asyncio as aioredis
from atlas_server.config import get_settings
from atlas_server.db.session import get_sessionmaker
from atlas_server.executor.inprocess import InProcessExecutor
from atlas_server.main import create_app
from atlas_server.services.approval import DEFAULT_TIMEOUT_S, decision_key, submit_decision
from httpx import ASGITransport
from langchain_core.messages import AIMessageChunk

from tests.fakes import TurnModel, tool_call_chunk
from tests.test_runs_api import wait_for_status

GUARDED_SPEC = {
    "system_prompt": "p",
    "model": {"model": "claude-opus-5"},
    "tool_names": ["write_todos"],
    "limits": {"require_approval_for": ["write_todos"]},
}


def make_app():
    app = create_app()
    app.state.executor = InProcessExecutor(
        get_sessionmaker(),
        get_settings(),
        model_builder=lambda *_a, **_k: TurnModel(
            scripts=[
                [tool_call_chunk("write_todos", '{"todos":[]}', "c1")],
                [AIMessageChunk(content="办完了")],
            ]
        ),
    )
    return app


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def redis() -> aioredis.Redis:
    client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    yield client
    await client.aclose()


async def _start_guarded_run(client: httpx.AsyncClient) -> tuple[str, str]:
    """发起一个会触发审批的 run，返回 (run_id, approval_id)。"""
    agent = await client.post(
        "/v1/agents", json={"slug": "guarded", "name": "受管控", "spec": GUARDED_SPEC}
    )
    assert agent.status_code == 201, agent.text
    thread = await client.post("/v1/threads", json={"agent_id": agent.json()["id"], "title": "t"})
    res = await client.post(
        f"/v1/threads/{thread.json()['id']}/runs",
        json={"content": [{"type": "text", "text": "记个待办"}]},
    )
    run_id = res.json()["run_id"]

    # 轮询待决端点而不是从 SSE 里抠 —— 中途 break 出 aiter_lines 会在服务端
    # xread 阻塞里掐断生成器，那是测试写法造成的噪音，不是被测行为。
    for _ in range(60):
        pending = (await client.get(f"/v1/runs/{run_id}/approvals")).json()["data"]
        if pending:
            return run_id, pending[0]["id"]
        await asyncio.sleep(0.1)
    raise AssertionError("没等到待决的确认项")


@pytest.mark.usefixtures("clean_db")
async def test_run_waits_and_marks_awaiting_approval(client: httpx.AsyncClient) -> None:
    """★ 执行器真的停在那里等 —— 而不是继续往下跑。"""
    run_id, _ = await _start_guarded_run(client)

    await asyncio.sleep(0.3)
    run = (await client.get(f"/v1/runs/{run_id}")).json()
    assert run["status"] == "awaiting_approval", f"实际 {run['status']}"


@pytest.mark.usefixtures("clean_db")
async def test_approve_resumes_the_run(client: httpx.AsyncClient) -> None:
    """★ 整条链路的接缝：HTTP 决策 → Redis → 阻塞中的执行器被唤醒。"""
    run_id, approval_id = await _start_guarded_run(client)

    res = await client.post(
        f"/v1/runs/{run_id}/approvals/{approval_id}", json={"decision": "approved"}
    )
    assert res.status_code == 200, res.text

    run = await wait_for_status(client, run_id)
    assert run["status"] == "succeeded", run


@pytest.mark.usefixtures("clean_db")
async def test_reject_lets_the_run_finish(client: httpx.AsyncClient) -> None:
    """§12.2：拒绝不终止 run。"""
    run_id, approval_id = await _start_guarded_run(client)

    await client.post(f"/v1/runs/{run_id}/approvals/{approval_id}", json={"decision": "rejected"})
    run = await wait_for_status(client, run_id)
    assert run["status"] == "succeeded", "拒绝把整轮弄失败了"


@pytest.mark.usefixtures("clean_db")
async def test_second_decision_conflicts(client: httpx.AsyncClient) -> None:
    """★ 多标签页 / 重复点击：只有第一次决策生效。"""
    run_id, approval_id = await _start_guarded_run(client)

    first = await client.post(
        f"/v1/runs/{run_id}/approvals/{approval_id}", json={"decision": "approved"}
    )
    second = await client.post(
        f"/v1/runs/{run_id}/approvals/{approval_id}", json={"decision": "rejected"}
    )

    assert first.status_code == 200
    assert second.status_code == 409, "重复决策没被拒"
    await wait_for_status(client, run_id)


@pytest.mark.usefixtures("clean_db")
async def test_decision_arriving_first_is_not_lost(redis: aioredis.Redis) -> None:
    """★ 用 BLPOP 而非 pub/sub 的理由。

    决策比等待方先到时，pub/sub 会把消息丢掉（无人订阅），执行器就会一直
    等到 10 分钟超时。队列不会 —— 消息躺在那儿等人取。
    """
    from uuid import uuid4

    aid = uuid4()
    key = decision_key(aid)
    await redis.delete(key)

    # 决策先到
    await redis.rpush(key, "approved")
    await redis.expire(key, DEFAULT_TIMEOUT_S)

    # 等待方后到，应当立刻拿到而不是阻塞
    item = await asyncio.wait_for(redis.blpop([key], timeout=5), timeout=2.0)
    assert item is not None and item[1] == "approved"


def test_submit_decision_is_exported() -> None:
    """端点用的就是它 —— 防止重构时被改名而没人发现。"""
    assert callable(submit_decision)
