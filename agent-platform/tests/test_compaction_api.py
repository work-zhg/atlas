"""P4 · 压缩的 server 侧接线（文档 §7.4）。

engine 侧（何时压、从哪切）见 test_compaction_wiring.py。
这里测的是持久化：**压缩只付一次钱**，以及 message 表不受影响。
"""

from __future__ import annotations

import httpx
import pytest
from atlas_agent.config import get_settings
from atlas_agent.db.models import Thread
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.executor.compose import build_executor
from atlas_agent.main import create_app
from atlas_agent.repositories.run import RunRepository
from atlas_agent.repositories.thread_context import ThreadContextRepository
from atlas_agent.services.compaction import make_persist_hook, thread_summarizer
from httpx import ASGITransport
from sqlalchemy import select

from tests.fake_uc import AS_USER
from tests.fakes import text_model
from tests.test_runs_api import wait_for_status


def make_app():
    app = create_app()
    app.state.executor = build_executor(
        get_sessionmaker(),
        get_settings(),
        model_builder=lambda *_a, **_k: text_model("好的。"),
    )
    return app


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AS_USER) as c:
        yield c


async def _thread_with_messages(client: httpx.AsyncClient, rounds: int) -> str:
    agent = await client.post(
        "/v1/agents",
        json={
            "slug": "compact-host",
            "name": "长会话",
            "spec": {"system_prompt": "p", "model": {"model": "claude-opus-5"}},
        },
    )
    assert agent.status_code == 201, agent.text
    thread = await client.post("/v1/threads", json={"agent_id": agent.json()["id"], "title": "t"})
    tid = thread.json()["id"]
    for i in range(rounds):
        res = await client.post(
            f"/v1/threads/{tid}/runs",
            json={"content": [{"type": "text", "text": f"第 {i} 问"}]},
        )
        await wait_for_status(client, res.json()["run_id"])
    return tid


@pytest.mark.usefixtures("clean_db")
async def test_summary_is_reused_not_recomputed(client: httpx.AsyncClient) -> None:
    """★ §7.4 的核心：摘要存下来后，下一轮不再重算。

    直接验持久化契约 —— thread 上有摘要时，history() 只返回边界之后的消息。
    """
    tid = await _thread_with_messages(client, rounds=3)

    async with get_sessionmaker()() as session:
        thread = (
            await session.execute(select(Thread).where(Thread.thread_uuid == tid))
        ).scalar_one()
        all_messages = await RunRepository(session).history(thread.thread_uuid)
        assert len(all_messages) >= 6, "没造出足够的消息"

        # 手工模拟一次压缩落库：保留区从第 4 条开始，前 3 条进了摘要
        await make_persist_hook(get_sessionmaker(), thread.thread_uuid)(
            {"summary": "早期摘要正文", "kept_from": str(all_messages[3].message_uuid)}
        )

    async with get_sessionmaker()() as session:
        context = await ThreadContextRepository(session).get(tid)
        assert context is not None
        assert context.summary == "早期摘要正文"
        assert context.compact_count == 1

        after = await RunRepository(session).history(tid, after=context.summary_upto)
        assert len(after) == len(all_messages) - 3, "边界之前的消息没有被排除"


@pytest.mark.usefixtures("clean_db")
async def test_message_table_is_never_touched(client: httpx.AsyncClient) -> None:
    """★ §7.4 两套存储：模型视角被压缩，**用户视角完整保留**。

    UI 滚到顶仍能看到第一条消息 —— 这是压缩可被用户理解的前提。
    """
    tid = await _thread_with_messages(client, rounds=3)
    before = (await client.get(f"/v1/threads/{tid}/messages")).json()["data"]

    async with get_sessionmaker()() as session:
        thread = (
            await session.execute(select(Thread).where(Thread.thread_uuid == tid))
        ).scalar_one()
        rows = await RunRepository(session).history(thread.thread_uuid)
        await make_persist_hook(get_sessionmaker(), thread.thread_uuid)(
            {"summary": "摘要", "kept_from": str(rows[3].message_uuid)}
        )

    after = (await client.get(f"/v1/threads/{tid}/messages")).json()["data"]
    assert len(after) == len(before), "压缩动了 message 表"
    assert after[-1]["content"] == before[-1]["content"]


@pytest.mark.usefixtures("clean_db")
async def test_compact_count_accumulates(client: httpx.AsyncClient) -> None:
    """§7.4：compact_count 累加，供监控与 UI 提示用。"""
    tid = await _thread_with_messages(client, rounds=2)

    async with get_sessionmaker()() as session:
        rows = await RunRepository(session).history(tid)
    hook = make_persist_hook(get_sessionmaker(), tid)
    await hook({"summary": "一", "kept_from": str(rows[2].message_uuid)})
    await hook({"summary": "二", "kept_from": str(rows[2].message_uuid)})

    async with get_sessionmaker()() as session:
        context = await ThreadContextRepository(session).get(tid)
        assert context is not None
        assert context.compact_count == 2
        assert context.summary == "二", "后一次摘要应覆盖前一次"


@pytest.mark.usefixtures("clean_db")
async def test_kept_turns_are_not_hidden_behind_the_boundary(client: httpx.AsyncClient) -> None:
    """★ 自动压缩保留最近几个回合不进摘要：下一轮载入的历史里必须还有它们。

    以前覆盖边界取「本次 run 起始时历史的最后一条」，保留的回合落在边界之前 —— 既不在摘要里、
    也不在原文里，模型就再也看不到它们了。
    """
    tid = await _thread_with_messages(client, rounds=3)  # 问 0 答 0 问 1 答 1 问 2 答 2
    async with get_sessionmaker()() as session:
        rows = await RunRepository(session).history(tid)
    kept = [r for r in rows if r.role == "user"][1]  # 保留后两个回合：从「第 1 问」开始

    await make_persist_hook(get_sessionmaker(), tid)(
        {"summary": "第 0 轮的摘要", "kept_from": str(kept.message_uuid)}
    )

    async with get_sessionmaker()() as session:
        context = await ThreadContextRepository(session).get(tid)
        assert context is not None
        after = await RunRepository(session).history(tid, after=context.summary_upto)
    assert after[0].message_uuid == kept.message_uuid
    assert [r.message_uuid for r in after] == [r.message_uuid for r in rows[rows.index(kept) :]]


@pytest.mark.usefixtures("clean_db")
async def test_no_boundary_no_summary(client: httpx.AsyncClient) -> None:
    """保留区起点不是库里的消息（或前面没有消息）：不存摘要，下一轮再压，不存猜出来的边界。"""
    from uuid import uuid4

    tid = await _thread_with_messages(client, rounds=2)
    async with get_sessionmaker()() as session:
        rows = await RunRepository(session).history(tid)
    hook = make_persist_hook(get_sessionmaker(), tid)
    await hook({"summary": "框架分配的 id", "kept_from": str(uuid4())})
    await hook({"summary": "没有起点"})
    await hook({"summary": "前面没有消息", "kept_from": str(rows[0].message_uuid)})

    async with get_sessionmaker()() as session:
        assert await ThreadContextRepository(session).get(tid) is None


@pytest.mark.usefixtures("clean_db")
async def test_persist_failure_does_not_raise() -> None:
    """摘要没存住只是下轮重压一次，不该让本轮 run 失败。"""
    from uuid import uuid4

    hook = make_persist_hook(get_sessionmaker(), uuid4())
    await hook({"summary": "x", "kept_from": str(uuid4())})  # thread 不存在：找不到边界，不存、不抛


async def test_summarizer_uses_injected_builder_and_endpoint() -> None:
    """★ 模型经 model_builder 注入，用给定的模型和端点（智能体的客户端 Key）。

    自己调 build_chat_model 会绕过注入点 —— P5 的标题生成踩过一次，
    表现是每个测试都真打网关、超时几秒再降级，测试时间从 7.7s 涨到 32.7s。
    """
    from atlas_agent.providers.llm.model_platform import ModelEndpoint
    from langchain_core.messages import HumanMessage

    seen: list[tuple[str, str, str]] = []

    def fake_builder(model_spec, *, base_url, api_key, **_kw):
        seen.append((model_spec.model, base_url, api_key))
        return text_model("这是摘要正文")

    endpoint = ModelEndpoint("http://gw/universal", "sk-agent")
    summarizer = thread_summarizer(fake_builder, "claude-haiku-4-5", endpoint=endpoint)
    result = await summarizer([HumanMessage(content="早期对话")])

    assert result == "这是摘要正文"
    assert seen == [("claude-haiku-4-5", "http://gw/universal", "sk-agent")]
