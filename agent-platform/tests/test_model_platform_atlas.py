"""Atlas 经模型管理平台（agent-platform/doc/design/model-design.html 第 7、9 章）：
建智能体时创建客户端 Key、编辑器的模型列表、保存时的校验、Run 开始的检查与端点、平台服务模型。

平台的内部接口用 MockTransport 顶替（FakePlatform）；模型换成记录参数的假模型（看每次调用
连到哪、带哪把 Key）。★ 用测试库：DATABASE_URL 要指向 atlas_test（见仓库里的测试约定）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from atlas_agent.config import get_settings
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.executor.inprocess import InProcessExecutor
from atlas_agent.main import create_app
from atlas_agent.providers.llm.model_platform import (
    ModelPlatformClient,
    reset_model_platform_cache,
)
from atlas_agent.services.service_models import reset_service_model_cache
from httpx import ASGITransport

from tests.fake_uc import AS_USER
from tests.fakes import text_model
from tests.test_runs_api import wait_for_status

LITELLM = "http://litellm.test:4000"
CATALOG = [
    {"name": "m1", "display_name": "模型一", "provider": "deepseek", "max_input_tokens": 131072, "max_output_tokens": 8192},
    {"name": "m2", "display_name": "模型二", "provider": "anthropic", "max_input_tokens": 200000, "max_output_tokens": 64000},
    {"name": "m-title", "display_name": "标题模型", "provider": "deepseek"},
]


class FakePlatform:
    """平台的内部接口：客户端 Key（类型 + 编码 → 状态、原文、可用模型）与已登记的模型。"""

    def __init__(self) -> None:
        self.keys: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []

    def assign(self, client_type: str, code: str, models: list[str], secret: str) -> None:
        key = self.keys.setdefault(
            (client_type, code),
            {"client_type": client_type, "client_code": code, "name": code},
        )
        key |= {
            "status": "active",
            "key": secret,
            "models": [m for m in CATALOG if m["name"] in models],
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if request.url.path == "/internal/models":
            return httpx.Response(200, json={"models": CATALOG})
        if request.method in ("POST", "PATCH"):
            slot = (body["client_type"], body["client_code"])
            key = self.keys.setdefault(
                slot,
                {
                    "client_type": slot[0],
                    "client_code": slot[1],
                    "status": "pending_models",
                    "key": None,
                    "models": [],
                },
            )
            key["name"] = body["name"]
            return httpx.Response(200, json=key)
        slot = (request.url.params["client_type"], request.url.params["client_code"])
        if slot not in self.keys:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json=self.keys[slot])


@pytest.fixture
def platform(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePlatform]:
    fake = FakePlatform()
    monkeypatch.setenv("MODEL_PLATFORM_URL", "http://platform.test")
    monkeypatch.setenv("MODEL_LITELLM_URL", LITELLM)
    get_settings.cache_clear()
    reset_model_platform_cache()
    reset_service_model_cache()
    ModelPlatformClient.transport = httpx.MockTransport(fake.handle)
    try:
        yield fake
    finally:
        ModelPlatformClient.transport = None
        reset_model_platform_cache()
        reset_service_model_cache()
        get_settings.cache_clear()


@pytest.fixture
def calls() -> list[dict[str, Any]]:
    """假模型被构造时的参数：spec、base_url、api_key。"""
    return []


@pytest.fixture
async def client(platform: FakePlatform, calls: list[dict[str, Any]]) -> AsyncIterator[httpx.AsyncClient]:
    def builder(spec: Any, *, base_url: str, api_key: str, **_: Any) -> Any:
        calls.append({"spec": spec, "base_url": base_url, "api_key": api_key})
        return text_model("回答" if spec.max_output_tokens != 64 else "生成的标题")

    app = create_app()
    app.state.executor = InProcessExecutor(get_sessionmaker(), get_settings(), model_builder=builder)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=AS_USER
    ) as c:
        yield c


async def _agent(client: httpx.AsyncClient, model: str = "m1") -> dict[str, Any]:
    r = await client.post(
        "/v1/agents",
        json={
            "slug": "platform-test",
            "name": "平台测试",
            "spec": {"system_prompt": "p", "model": {"model": model}},
        },
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


async def _run(client: httpx.AsyncClient, agent_id: str) -> dict[str, Any]:
    thread = (await client.post("/v1/threads", json={"agent_id": agent_id})).json()
    res = await client.post(
        f"/v1/threads/{thread['id']}/runs", json={"content": [{"type": "text", "text": "你好"}]}
    )
    assert res.status_code == 202, res.text
    await wait_for_status(client, res.json()["run_id"])
    run = (await client.get(f"/v1/runs/{res.json()['run_id']}")).json()
    return dict(run) | {"thread_id": thread["id"]}


@pytest.mark.usefixtures("clean_db")
async def test_creating_an_agent_creates_its_client_key(
    client: httpx.AsyncClient, platform: FakePlatform
) -> None:
    agent = await _agent(client)
    key = platform.keys[("Agent", agent["id"])]
    assert key["status"] == "pending_models" and key["name"] == "平台测试"

    renamed = await client.patch(f"/v1/agents/{agent['id']}", json={"name": "新名字"})
    assert renamed.status_code == 200, renamed.text
    assert platform.keys[("Agent", agent["id"])]["name"] == "新名字"


@pytest.mark.usefixtures("clean_db")
async def test_editor_lists_registered_models_until_assigned(
    client: httpx.AsyncClient, platform: FakePlatform
) -> None:
    fresh = (await client.get("/v1/models")).json()
    assert fresh["key"]["status"] == "none"
    assert [m["model"] for m in fresh["data"]] == ["m1", "m2", "m-title"]
    assert fresh["data"][0]["context_window"] == 131072

    agent = await _agent(client)
    pending = (await client.get("/v1/models", params={"agent_id": agent["id"]})).json()
    assert pending["key"]["status"] == "pending_models"
    assert len(pending["data"]) == 3 and "还没有分配模型" in pending["key"]["notes"][0]

    platform.assign("Agent", agent["id"], ["m1"], "sk-agent")
    active = (await client.get("/v1/models", params={"agent_id": agent["id"]})).json()
    assert active["key"]["status"] == "active"
    assert [m["model"] for m in active["data"]] == ["m1"]


@pytest.mark.usefixtures("clean_db")
async def test_saving_checks_new_models_against_the_key(
    client: httpx.AsyncClient, platform: FakePlatform
) -> None:
    unknown = await client.post(
        "/v1/agents",
        json={
            "slug": "platform-unknown",
            "name": "未登记的模型",
            "spec": {"system_prompt": "p", "model": {"model": "nope"}},
        },
    )
    assert unknown.status_code == 400
    agent = await _agent(client)
    platform.assign("Agent", agent["id"], ["m1"], "sk-agent")
    outside = await client.patch(
        f"/v1/agents/{agent['id']}", json={"spec": {"system_prompt": "p", "model": {"model": "m2"}}}
    )
    assert outside.status_code == 400
    assert "m2" in outside.text


@pytest.mark.usefixtures("clean_db")
async def test_run_waits_for_assignment_then_calls_litellm_with_the_agent_key(
    client: httpx.AsyncClient, platform: FakePlatform, calls: list[dict[str, Any]]
) -> None:
    agent = await _agent(client)
    blocked = await _run(client, agent["id"])
    assert blocked["status"] == "failed"
    assert not [c for c in calls if c["spec"].max_output_tokens != 64], "没分配模型就不该调模型"

    platform.assign("Agent", agent["id"], ["m1"], "sk-agent")
    done = await _run(client, agent["id"])
    assert done["status"] == "succeeded", done
    main = [c for c in calls if c["spec"].max_output_tokens != 64]
    assert main and main[0]["base_url"] == LITELLM and main[0]["api_key"] == "sk-agent"
    # 经平台一律走 Anthropic Messages（LiteLLM 转换）
    assert main[0]["spec"].provider == "anthropic"


@pytest.mark.usefixtures("clean_db")
async def test_title_uses_the_configured_service_model(
    client: httpx.AsyncClient, platform: FakePlatform, calls: list[dict[str, Any]]
) -> None:
    # 没在平台上配好之前，设置会当场被拒
    early = await client.put(
        "/v1/service-models/title",
        json={"client_type": "Service", "client_code": "session-title", "model": "m-title"},
    )
    assert early.status_code == 400

    platform.assign("Service", "session-title", ["m-title"], "sk-title")
    ok = await client.put(
        "/v1/service-models/title",
        json={"client_type": "Service", "client_code": "session-title", "model": "m-title"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["ready"] is True

    agent = await _agent(client)
    platform.assign("Agent", agent["id"], ["m1"], "sk-agent")
    run = await _run(client, agent["id"])
    title_calls = [c for c in calls if c["spec"].max_output_tokens == 64]
    assert title_calls and title_calls[0]["api_key"] == "sk-title"
    assert title_calls[0]["spec"].model == "m-title"
    # 显式关思考：DeepSeek 默认思考，64 个 token 会全花在思考块上
    assert title_calls[0]["spec"].thinking == "off"
    thread =(await client.get(f"/v1/threads/{run['thread_id']}")).json()
    assert thread["title"] == "生成的标题"

    listed = (await client.get("/v1/service-models")).json()
    assert {v["purpose"]: v["ready"] for v in listed} == {
        "title": True,
        "memory_backfill": False,
        "approval_judge": False,
    }
