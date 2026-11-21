"""GET /v1/models 与 /v1/tools —— P0 的完成标准（文档 §16）。

需要 Postgres（迁移已跑）。模型列表来自模型管理平台（conftest 接的替身），能力来自
model_catalog 表；平台不可达时应降级而非报错。
"""

from __future__ import annotations

import httpx
import pytest
from atlas_agent.main import create_app
from httpx import ASGITransport

from tests.fake_model_platform import MODELS
from tests.fake_uc import AS_USER


@pytest.fixture
async def client() -> httpx.AsyncClient:
    app = create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AS_USER) as c:
        yield c


async def test_healthz(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_models_come_from_the_model_platform(client: httpx.AsyncClient) -> None:
    """新建智能体（还没有 Key）：列平台上登记的全部模型。"""
    resp = await client.get("/v1/models")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [m["model"] for m in body["data"]] == list(MODELS)
    assert body["gateway_reachable"] is True


async def test_model_metadata_matches_measured_values(client: httpx.AsyncClient) -> None:
    """锁住 2026-08-19 核实过的上下文窗口 / 输出上限（曾经写错过）。"""
    body = (await client.get("/v1/models")).json()
    by_id = {m["model"]: m for m in body["data"]}

    for model in ("claude-opus-5", "claude-sonnet-5", "claude-fable-5"):
        assert by_id[model]["context_window"] == 1_000_000, model
        assert by_id[model]["max_output_tokens"] == 128_000, model
        # temperature 在这些模型上是硬 400
        assert by_id[model]["supports_temperature"] is False, model

        # adaptive thinking 与 effort 都是 4.6+ 才有的
        assert by_id[model]["supports_adaptive_thinking"] is True, model
        assert by_id[model]["supports_effort"] is True, model

    haiku = by_id["claude-haiku-4-5"]
    assert haiku["context_window"] == 200_000
    assert haiku["max_output_tokens"] == 64_000
    assert haiku["supports_temperature"] is True
    # ★ 支持思考 ≠ 支持 adaptive 模式：haiku 传 adaptive 会 400
    assert haiku["supports_thinking"] is True
    assert haiku["supports_adaptive_thinking"] is False
    # ★ haiku 也不收 effort —— 它是"旧模型"档：temperature 可用，effort/adaptive 不可用
    assert haiku["supports_effort"] is False

    # 最小可缓存前缀非单调：opus-5 < sonnet-5 < haiku-4-5
    assert by_id["claude-opus-5"]["min_cacheable_tokens"] == 512
    assert by_id["claude-sonnet-5"]["min_cacheable_tokens"] == 1024
    assert haiku["min_cacheable_tokens"] == 4096


async def test_models_degrades_when_platform_unreachable(client: httpx.AsyncClient) -> None:
    """模型管理平台不可达不该让 /models 挂 —— gateway_reachable 置 false，并说明原因。"""
    from atlas_agent.providers.llm.model_platform import ModelPlatformClient

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    ModelPlatformClient.transport = httpx.MockTransport(down)
    resp = await client.get("/v1/models")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["gateway_reachable"] is False
    assert body["data"] == []
    assert body["key"]["notes"]


async def test_tools_catalog_availability(client: httpx.AsyncClient) -> None:
    """目录不说谎：available 反映 build_graph 的真实行为。

    bash 要部署开启沙箱（SANDBOX_ENABLED）才可用 —— 测试环境没开，目录必须照实说
    「不可用」并给出原因，否则用户勾了它才发现 execute 不存在。
    """
    body = (await client.get("/v1/tools")).json()
    by_name = {t["name"]: t for t in body["data"]}
    assert by_name["bash"]["available"] is False
    assert "SANDBOX_ENABLED" in by_name["bash"]["note"]
    assert "coding_task" not in by_name

    # ★ 网页搜索已改走 MCP（MCP 网关上的 serpapi），不再是内置工具
    assert "web_search" not in by_name

    # 无外部前提的工具恒可用
    assert by_name["write_todos"]["available"] is True
    assert by_name["filesystem"]["available"] is True
