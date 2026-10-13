"""P4 · 压缩的 server 侧接线（文档 §7.4）。

engine 侧（何时压、从哪切）见 test_compaction_wiring.py。
这里测的是持久化：**压缩只付一次钱**，以及 message 表不受影响。
"""

from __future__ import annotations

import httpx
import pytest
from atlas_server.config import get_settings
from atlas_server.db.models import Thread
from atlas_server.db.session import get_sessionmaker
from atlas_server.executor.inprocess import InProcessExecutor
from atlas_server.main import create_app
from atlas_server.repositories.run import RunRepository
from atlas_server.services.compaction import ThreadSummarizer, make_persist_hook
from httpx import ASGITransport
from sqlalchemy import select

from tests.fakes import text_model
from tests.test_runs_api import wait_for_status


def make_app():
    app = create_app()
    app.state.executor = InProcessExecutor(
        get_sessionmaker(),
        get_settings(),
        model_builder=lambda *_a, **_k: text_model("好的。"),
    )
    return app


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
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
        thread = (await session.execute(select(Thread).where(Thread.id == tid))).scalar_one()
        all_messages = await RunRepository(session).history(thread.id)
        assert len(all_messages) >= 6, "没造出足够的消息"

        # 手工模拟一次压缩落库
        cutoff = all_messages[2].created_at
        await make_persist_hook(get_sessionmaker(), thread.id, cutoff)({"summary": "早期摘要正文"})

    async with get_sessionmaker()() as session:
        thread = (await session.execute(select(Thread).where(Thread.id == tid))).scalar_one()
        assert thread.summary == "早期摘要正文"
        assert thread.compact_count == 1

        after = await RunRepository(session).history(thread.id, after=thread.summary_upto)
        assert len(after) == len(all_messages) - 3, "边界之前的消息没有被排除"


@pytest.mark.usefixtures("clean_db")
async def test_message_table_is_never_touched(client: httpx.AsyncClient) -> None:
    """★ §7.4 两套存储：模型视角被压缩，**用户视角完整保留**。

    UI 滚到顶仍能看到第一条消息 —— 这是压缩可被用户理解的前提。
    """
    tid = await _thread_with_messages(client, rounds=3)
    before = (await client.get(f"/v1/threads/{tid}/messages")).json()["data"]

    async with get_sessionmaker()() as session:
        thread = (await session.execute(select(Thread).where(Thread.id == tid))).scalar_one()
        rows = await RunRepository(session).history(thread.id)
        await make_persist_hook(get_sessionmaker(), thread.id, rows[2].created_at)(
            {"summary": "摘要"}
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
    hook = make_persist_hook(get_sessionmaker(), tid, rows[1].created_at)
    await hook({"summary": "一"})
    await hook({"summary": "二"})

    async with get_sessionmaker()() as session:
        thread = (await session.execute(select(Thread).where(Thread.id == tid))).scalar_one()
        assert thread.compact_count == 2
        assert thread.summary == "二", "后一次摘要应覆盖前一次"


@pytest.mark.usefixtures("clean_db")
async def test_persist_failure_does_not_raise() -> None:
    """摘要没存住只是下轮重压一次，不该让本轮 run 失败。"""
    from datetime import UTC, datetime
    from uuid import uuid4

    hook = make_persist_hook(get_sessionmaker(), uuid4(), datetime.now(UTC))
    await hook({"summary": "x"})  # thread 不存在，UPDATE 影响 0 行，不抛


async def test_summarizer_uses_injected_builder_and_haiku() -> None:
    """★ 模型经 model_builder 注入，且固定用 summarizer_model。

    自己调 build_chat_model 会绕过注入点 —— P5 的标题生成踩过一次，
    表现是每个测试都真打网关、超时几秒再降级，测试时间从 7.7s 涨到 32.7s。

    模型选择也要断言：摘要是一次**输入很大**的调用，用 opus 代价明显，
    而它不需要 opus 的推理能力。
    """
    from langchain_core.messages import HumanMessage

    seen: list[str] = []

    def fake_builder(model_spec, **_kw):
        seen.append(model_spec.model)
        return text_model("这是摘要正文")

    settings = get_settings()
    summarizer = ThreadSummarizer(settings, fake_builder)
    result = await summarizer([HumanMessage(content="早期对话")])

    assert result == "这是摘要正文"
    assert seen == [settings.summarizer_model], f"用了 {seen}，应固定为 summarizer_model"
