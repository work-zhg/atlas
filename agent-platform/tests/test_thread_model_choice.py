"""对话框的模型选择器：会话记住用户选的主模型，开跑时换进智能体的配置。

★ 可选的模型按智能体的 Key 列出（GET /v1/models），能不能用在开跑时按同一把 Key 检查 ——
  这里只测「选了什么、记住了什么、这一轮实际用了什么」。
★ CLI 智能体的模型在拉起 CLI 时就定了，对话里不能换（要换去智能体配置里改）。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from atlas_agent.config import get_settings
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.executor.compose import build_executor
from atlas_agent.main import create_app
from httpx import ASGITransport
from langchain_core.messages import AIMessageChunk

from tests.fake_uc import AS_USER
from tests.fakes import TurnModel
from tests.test_runs_api import wait_for_status

AGENT_MODEL = "claude-opus-5"
PICKED = "claude-sonnet-5"


@pytest.fixture
def built() -> list[str]:
    """每次装配模型时的模型名（主模型；没有标题的会话还会多一次标题模型）。"""
    return []


@pytest.fixture
async def client(built: list[str]) -> httpx.AsyncClient:
    def model_builder(spec: Any, **_kw: Any) -> TurnModel:
        built.append(spec.model)
        return TurnModel(scripts=[[AIMessageChunk(content="好")]])

    app = create_app()
    app.state.executor = build_executor(
        get_sessionmaker(), get_settings(), model_builder=model_builder
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AS_USER) as c:
        yield c


async def _thread(client: httpx.AsyncClient, spec: dict[str, Any], slug: str) -> str:
    r = await client.post("/v1/agents", json={"slug": slug, "name": slug, "spec": spec})
    assert r.status_code == 201, r.text
    r = await client.post("/v1/threads", json={"agent_id": r.json()["id"], "title": "t"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _send(client: httpx.AsyncClient, thread_id: str, **extra: Any) -> httpx.Response:
    return await client.post(
        f"/v1/threads/{thread_id}/runs",
        json={"content": [{"type": "text", "text": "hi"}], **extra},
    )


async def _run(client: httpx.AsyncClient, thread_id: str, **extra: Any) -> None:
    r = await _send(client, thread_id, **extra)
    assert r.status_code == 202, r.text
    assert (await wait_for_status(client, r.json()["run_id"]))["status"] == "succeeded"


NATIVE = {"system_prompt": "p", "model": {"model": AGENT_MODEL}}


async def test_picked_model_is_used_and_remembered(
    client: httpx.AsyncClient, built: list[str], clean_db: None
) -> None:
    thread_id = await _thread(client, NATIVE, "pick-model")
    detail = (await client.get(f"/v1/threads/{thread_id}")).json()
    assert detail["model"] is None
    assert detail["model_choice"] == {
        "model": AGENT_MODEL,
        "default_model": AGENT_MODEL,
        "switchable": True,
    }

    await _run(client, thread_id, model=PICKED)
    assert PICKED in built and AGENT_MODEL not in built

    detail = (await client.get(f"/v1/threads/{thread_id}")).json()
    assert detail["model"] == PICKED
    assert detail["model_choice"]["model"] == PICKED
    assert detail["model_choice"]["default_model"] == AGENT_MODEL

    # 不传 = 沿用会话记住的
    built.clear()
    await _run(client, thread_id)
    assert built == [PICKED]


async def test_picking_the_agent_model_follows_the_agent_again(
    client: httpx.AsyncClient, built: list[str], clean_db: None
) -> None:
    thread_id = await _thread(client, NATIVE, "follow-agent")
    await _run(client, thread_id, model=PICKED)

    built.clear()
    await _run(client, thread_id, model=AGENT_MODEL)
    assert built == [AGENT_MODEL]
    detail = (await client.get(f"/v1/threads/{thread_id}")).json()
    assert detail["model"] is None  # 记成「跟随智能体」：智能体以后改了模型，会话跟着变
    assert detail["model_choice"]["model"] == AGENT_MODEL


async def test_cli_agent_cannot_switch_model_in_chat(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    spec = {"kind": "cli", "model": {"model": AGENT_MODEL}, "cli": {"cli_type": "claude-code"}}
    thread_id = await _thread(client, spec, "cli-model")

    detail = (await client.get(f"/v1/threads/{thread_id}")).json()
    assert detail["model_choice"] == {
        "model": AGENT_MODEL,
        "default_model": AGENT_MODEL,
        "switchable": False,
    }

    r = await _send(client, thread_id, model=PICKED)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["kind"] == "invalid_model_choice"
    # 被拒的请求不留下用户消息
    assert (await client.get(f"/v1/threads/{thread_id}/messages")).json()["data"] == []
