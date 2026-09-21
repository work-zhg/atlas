"""GET /v1/models 与 /v1/tools —— P0 的完成标准（文档 §16）。

需要 Postgres（迁移已跑）。网关不可达时应降级而非报错。
"""

from __future__ import annotations

import httpx
import pytest
from atlas_server.main import create_app
from httpx import ASGITransport

#: 迁移种子里的全部模型（0001 的 claude-* + 0008 的 deepseek-*）。
#:
#: ★ 钉的是**产品知道哪些模型的能力**，不是"这个部署能用哪些" —— 后者是
#:   is_available，由网关巡检刷新，随部署而变，不该进这个集合。
EXPECTED_MODELS = {
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-haiku-4-5",
    "deepseek-flash",
    "deepseek-v4-pro",
}


@pytest.fixture
async def client() -> httpx.AsyncClient:
    app = create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_healthz(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_models_returns_seeded_catalog(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/models")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {m["model"] for m in body["data"]} == EXPECTED_MODELS


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


async def test_models_degrades_when_gateway_unreachable(client: httpx.AsyncClient) -> None:
    """网关不可达不该让 /models 挂 —— 只把 gateway_reachable 置 false。"""
    body = (await client.get("/v1/models")).json()
    assert isinstance(body["gateway_reachable"], bool)
    # 无论巡检成败，catalog 都要整份返回（可用性可以全 false，但不能少行）
    assert len(body["data"]) == len(EXPECTED_MODELS)


async def test_tools_catalog_availability(client: httpx.AsyncClient) -> None:
    """目录不说谎：available 反映 build_graph 的真实行为。

    bash 的 Docker 实现已删除、K8s Pod 未接入 —— 目录必须照实说
    「不可用」并给出原因，否则用户勾了它才发现 execute 不存在。
    """
    body = (await client.get("/v1/tools")).json()
    by_name = {t["name"]: t for t in body["data"]}
    assert by_name["bash"]["available"] is False
    assert "K8s" in by_name["bash"]["note"]
    assert "coding_task" not in by_name

    # ★ web_search 的可用性取决于服务端有没有 SERPAPI_KEY —— 断言的是
    #   那条**规则**，不是本机恰好怎么配。写死 True 的话，没配 key 的
    #   开发机上这条会红，而它报的并不是回归。
    from atlas_server.config import get_settings

    expected = get_settings().serpapi_key is not None
    assert by_name["web_search"]["available"] is expected
    if not expected:
        assert "SERPAPI_KEY" in by_name["web_search"]["note"]

    # 无外部前提的工具恒可用
    assert by_name["write_todos"]["available"] is True
    assert by_name["filesystem"]["available"] is True
