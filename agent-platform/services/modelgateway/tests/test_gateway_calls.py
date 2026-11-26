"""模型网关端到端：测试库里的账号、模型、线路、Key + 假供应商。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
from atlas_modelgateway.db.models import GwModelCall
from atlas_modelgateway.db.session import get_sessionmaker
from atlas_modelgateway.gateway.app import GatewayRuntime, build_runtime, create_gateway_app
from atlas_modelgateway.gateway.format import AddressGuard
from atlas_modelgateway.settings import get_settings
from atlas_tenancy import TenantScope
from gw_testkit import (
    TENANT_A,
    TENANT_B,
    FakeUpstream,
    RouteSpec,
    anthropic_events,
    anthropic_json,
    error,
    seed,
    sse,
)
from sqlalchemy import select


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
async def gw(upstream: FakeUpstream) -> AsyncIterator[tuple[httpx.AsyncClient, GatewayRuntime]]:
    settings = get_settings().model_copy(
        update={"keepalive_interval_s": 0.05, "first_content_timeout_s": 2.0, "idle_timeout_s": 2.0}
    )
    runtime = build_runtime(
        settings, transport=upstream.transport(), guard=AddressGuard(enabled=False)
    )
    app = create_gateway_app(settings, runtime=runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client,
    ):
        yield client, runtime
    await runtime.upstream.aclose()


def _msg(model: str = "m1", **extra: Any) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": "你好"}],
        **extra,
    }


def _events(text: str) -> list[tuple[str | None, Any]]:
    out, name = [], None
    for line in text.splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            out.append((name, json.loads(line[6:])))
            name = None
    return out


async def _records(runtime: GatewayRuntime, tenant: Any = TENANT_A) -> list[GwModelCall]:
    await runtime.records.flush()
    with TenantScope.enter(tenant):
        async with get_sessionmaker()() as session:
            return list(await session.scalars(select(GwModelCall).order_by(GwModelCall.started_at)))


# ── 直通 ───────────────────────────────────────────────────────────────


async def test_passthrough_rewrites_params_and_restores_model(gw, upstream: FakeUpstream) -> None:
    client, runtime = gw
    s = await seed(
        TENANT_A,
        routes=[RouteSpec("a.test")],
        price={"input": "4", "cache_hit": "0.4", "output": "16"},
    )
    upstream.on("a.test", anthropic_json(usage_in=2000, usage_out=800, cache_hit=48000))
    r = await client.post(
        "/v1/messages",
        headers={"x-api-key": s.secret},
        json=_msg(temperature=0.2, top_p=0.9, extra_field="kept"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "m1"
    sent = upstream.hits("a.test")[0]
    assert sent.path == "/anthropic/v1/messages"
    assert sent.body["model"] == "claude-opus-5"
    assert "temperature" not in sent.body and "top_p" not in sent.body  # opus-5 不收
    assert sent.body["extra_field"] == "kept"  # 不认识的字段原样带过去
    assert sent.headers["x-api-key"] == "upstream-secret-a.test"
    [rec] = await _records(runtime)
    assert (rec.status, rec.input_tokens, rec.cache_hit_tokens, rec.output_tokens) == (
        "ok",
        2000,
        48000,
        800,
    )
    assert rec.cost == Decimal("0.040000")
    assert rec.notes["params"]


async def test_stream_passthrough(gw, upstream: FakeUpstream) -> None:
    client, runtime = gw
    s = await seed(TENANT_A, routes=[RouteSpec("a.test")])
    upstream.on("a.test", sse(anthropic_events("你好呀", usage_in=50, usage_out=7)))
    r = await client.post(
        "/v1/messages", headers={"authorization": f"Bearer {s.secret}"}, json=_msg(stream=True)
    )
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = _events(r.text)
    assert events[0][1]["message"]["model"] == "m1"
    assert [e[0] for e in events][-1] == "message_stop"
    [rec] = await _records(runtime)
    assert (rec.input_tokens, rec.output_tokens, rec.estimated) == (50, 7, False)


# ── 换线路 ──────────────────────────────────────────────────────────────


async def test_switch_route_on_error_after_200_before_content(gw, upstream: FakeUpstream) -> None:
    client, runtime = gw
    s = await seed(
        TENANT_A, routes=[RouteSpec("a.test", priority=1), RouteSpec("b.test", priority=2)]
    )
    overload = [
        anthropic_events()[0],
        (
            "error",
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        ),
    ]
    upstream.on("a.test", sse(overload))
    upstream.on("b.test", sse(anthropic_events("来自 B")))
    r = await client.post("/v1/messages", headers={"x-api-key": s.secret}, json=_msg(stream=True))
    assert r.status_code == 200
    texts = [e[1]["delta"]["text"] for e in _events(r.text) if e[0] == "content_block_delta"]
    assert texts == ["来自 B"]
    assert "error" not in [e[0] for e in _events(r.text)]
    [rec] = await _records(runtime)
    assert [a["outcome"] for a in rec.attempts] == ["overloaded", "ok"]
    # 线路 1 冷却中：下一次直接走线路 2
    upstream.on("b.test", sse(anthropic_events("又是 B")))
    await client.post("/v1/messages", headers={"x-api-key": s.secret}, json=_msg(stream=True))
    assert len(upstream.hits("a.test")) == 1


async def test_402_marks_account_abnormal(gw, upstream: FakeUpstream) -> None:
    client, runtime = gw
    s = await seed(
        TENANT_A,
        routes=[
            RouteSpec("a.test", provider="deepseek", upstream_model="deepseek-v4-pro"),
            RouteSpec("b.test", priority=2),
        ],
    )
    upstream.on("a.test", error(402, {"error": {"message": "Insufficient Balance"}}))
    upstream.on("b.test", anthropic_json("B"))
    for _ in range(2):
        r = await client.post("/v1/messages", headers={"x-api-key": s.secret}, json=_msg())
        assert r.status_code == 200
    assert len(upstream.hits("a.test")) == 1
    issues = await runtime.health.abnormal_accounts()
    assert issues[s.accounts["a.test"]].issue.value == "no_balance"


async def test_all_routes_rate_limited(gw, upstream: FakeUpstream) -> None:
    client, _ = gw
    s = await seed(TENANT_A, routes=[RouteSpec("a.test"), RouteSpec("b.test")])
    upstream.on(
        "a.test",
        error(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}),
    )
    upstream.on(
        "b.test",
        error(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}),
    )
    r = await client.post("/v1/messages", headers={"x-api-key": s.secret}, json=_msg(stream=True))
    assert r.status_code == 429
    assert r.headers["x-should-retry"] == "false"
    assert r.json()["error"]["category"] == "model_busy"


def _empty_stream() -> list[tuple[str, Any]]:
    """上游 200 但一个内容块都没有（MiniMax 偶发）。"""
    return [e for e in anthropic_events() if not e[0].startswith("content_block")]


async def test_single_route_failure_leaves_retry_to_sdk(gw, upstream: FakeUpstream) -> None:
    client, runtime = gw
    s = await seed(TENANT_A, routes=[RouteSpec("a.test")])
    upstream.on("a.test", sse(_empty_stream()))
    r = await client.post("/v1/messages", headers={"x-api-key": s.secret}, json=_msg(stream=True))
    assert r.status_code == 502
    assert r.json()["error"]["category"] == "upstream_error"
    assert "x-should-retry" not in r.headers  # 没换成线路：交给 SDK 重试
    [rec] = await _records(runtime)
    assert [a["outcome"] for a in rec.attempts] == ["overloaded"]


async def test_switched_routes_all_failing_tells_sdk_not_to_retry(
    gw, upstream: FakeUpstream
) -> None:
    client, _ = gw
    s = await seed(TENANT_A, routes=[RouteSpec("a.test"), RouteSpec("b.test")])
    upstream.on("a.test", sse(_empty_stream()))
    upstream.on("b.test", sse(_empty_stream()))
    r = await client.post("/v1/messages", headers={"x-api-key": s.secret}, json=_msg(stream=True))
    assert r.status_code == 502
    assert r.headers["x-should-retry"] == "false"


async def test_bad_request_is_not_retried_and_passes_upstream_body(
    gw, upstream: FakeUpstream
) -> None:
    client, _ = gw
    s = await seed(TENANT_A, routes=[RouteSpec("a.test"), RouteSpec("b.test")])
    body = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "messages.0: bad"},
    }
    upstream.on("a.test", error(400, body))
    upstream.on("b.test", error(400, body))
    r = await client.post("/v1/messages", headers={"x-api-key": s.secret}, json=_msg())
    assert r.status_code == 400 and r.json() == body
    assert len(upstream.seen) == 1


async def test_no_routes_left_when_accounts_abnormal(gw, upstream: FakeUpstream) -> None:
    client, _ = gw
    s = await seed(
        TENANT_A,
        routes=[RouteSpec("a.test", provider="deepseek", upstream_model="deepseek-v4-pro")],
    )
    upstream.on("a.test", error(402, {"error": {"message": "Insufficient Balance"}}))
    r1 = await client.post("/v1/messages", headers={"x-api-key": s.secret}, json=_msg())
    r2 = await client.post("/v1/messages", headers={"x-api-key": s.secret}, json=_msg())
    assert r1.status_code == r2.status_code == 503
    assert r2.json()["error"]["message"] == "这个模型暂时用不了，请换个模型"
    assert len(upstream.seen) == 1  # 第二次没打到供应商


# ── 准入、能力 ──────────────────────────────────────────────────────────


async def test_admission_and_capability_rejections(gw, upstream: FakeUpstream) -> None:
    client, _ = gw
    s = await seed(TENANT_A, routes=[RouteSpec("a.test")])
    r = await client.post("/v1/messages", headers={"x-api-key": "sk-wrong"}, json=_msg())
    assert r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"
    r = await client.post("/v1/messages", headers={"x-api-key": s.secret}, json=_msg("m2"))
    assert r.status_code == 403
    image = {
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
            }
        ],
    }
    r = await client.post(
        "/v1/messages", headers={"x-api-key": s.secret}, json={**_msg(), "messages": [image]}
    )
    assert r.status_code == 400 and "不能看图" in r.json()["error"]["message"]
    assert upstream.seen == []


async def test_budget_is_a_soft_limit(gw, upstream: FakeUpstream) -> None:
    client, runtime = gw
    s = await seed(
        TENANT_A,
        routes=[RouteSpec("a.test")],
        price={"input": "1000000", "output": "0"},
        budget=Decimal("1"),
    )
    upstream.on("a.test", anthropic_json(usage_in=2, usage_out=1))
    assert (
        await client.post("/v1/messages", headers={"x-api-key": s.secret}, json=_msg())
    ).status_code == 200
    await runtime.records.flush()
    import asyncio

    await asyncio.sleep(0.05)  # 花费在后台累加
    r = await client.post("/v1/messages", headers={"x-api-key": s.secret}, json=_msg())
    assert r.status_code == 429 and r.json()["error"]["category"] == "key_limit"


async def test_tenants_do_not_see_each_others_models(gw, upstream: FakeUpstream) -> None:
    client, _ = gw
    a = await seed(TENANT_A, routes=[RouteSpec("a.test")])
    await seed(TENANT_B, routes=[RouteSpec("b.test")])
    upstream.on("a.test", anthropic_json("A"))
    upstream.on("b.test", anthropic_json("B"))
    r = await client.post("/v1/messages", headers={"x-api-key": a.secret}, json=_msg())
    assert r.json()["content"][0]["text"] == "A"
    assert upstream.hits("b.test") == []


# ── 通用接口：转换 ──────────────────────────────────────────────────────


async def test_universal_to_openai_upstream_stream(gw, upstream: FakeUpstream) -> None:
    client, runtime = gw
    s = await seed(
        TENANT_A,
        routes=[
            RouteSpec("v.test", provider="deepseek", fmt="openai", upstream_model="deepseek-v4-pro")
        ],
    )
    chunks = [
        (
            None,
            {
                "id": "c",
                "model": "deepseek-v4-pro",
                "choices": [{"index": 0, "delta": {"reasoning_content": "想想"}}],
            },
        ),
        (None, {"choices": [{"index": 0, "delta": {"content": "答案"}}]}),
        (
            None,
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "read", "arguments": '{"a": 1}'},
                                }
                            ]
                        },
                    }
                ]
            },
        ),
        (None, {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
        (
            None,
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 120,
                    "prompt_cache_hit_tokens": 100,
                    "prompt_cache_miss_tokens": 20,
                    "completion_tokens": 9,
                },
            },
        ),
        (None, "[DONE]"),
    ]
    upstream.on("v.test", sse(chunks))
    body = _msg(
        stream=True, tools=[{"name": "read", "input_schema": {"type": "object"}}], system="你是助手"
    )
    r = await client.post("/universal/v1/messages", headers={"x-api-key": s.secret}, json=body)
    assert r.status_code == 200, r.text
    sent = upstream.hits("v.test")[0]
    assert sent.path == "/openai/chat/completions"
    assert sent.body["messages"][0] == {"role": "system", "content": "你是助手"}
    assert sent.body["stream_options"] == {"include_usage": True}
    events = _events(r.text)
    blocks = [e[1]["content_block"]["type"] for e in events if e[0] == "content_block_start"]
    assert blocks == ["thinking", "text", "tool_use"]
    sigs = [
        e[1]["delta"]["signature"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "signature_delta"
    ]
    assert sigs and sigs[0].startswith("atlas1:")
    final = next(e[1] for e in events if e[0] == "message_delta")
    assert final["delta"]["stop_reason"] == "tool_use"
    [rec] = await _records(runtime)
    assert (rec.input_tokens, rec.cache_hit_tokens, rec.output_tokens) == (20, 100, 9)
    assert rec.attempts[0]["path"] == "convert:openai"


# ── 保活、模型清单、数 token ────────────────────────────────────────────


async def test_keepalive_before_first_content(gw, upstream: FakeUpstream) -> None:
    client, _ = gw
    s = await seed(TENANT_A, routes=[RouteSpec("a.test")])
    upstream.on("a.test", sse(anthropic_events(), delay_s=0.12))
    r = await client.post("/v1/messages", headers={"x-api-key": s.secret}, json=_msg(stream=True))
    assert r.status_code == 200
    assert ": keepalive" in r.text
    assert _events(r.text)[-1][0] == "message_stop"


async def test_first_content_timeout_then_error_event(gw, upstream: FakeUpstream) -> None:
    client, runtime = gw
    s = await seed(TENANT_A, routes=[RouteSpec("a.test", first_content_timeout_s=1)])
    upstream.on("a.test", sse(anthropic_events(), delay_s=0.6))
    r = await client.post("/v1/messages", headers={"x-api-key": s.secret}, json=_msg(stream=True))
    # 已经发过保活注释（200 已经发出），只能在流里报错
    assert r.status_code == 200
    events = _events(r.text)
    assert events[-1][0] == "error" and events[-1][1]["error"]["category"] == "upstream_timeout"
    [rec] = await _records(runtime)
    assert rec.status == "error" and rec.attempts[0]["outcome"] == "timeout"


async def test_models_and_count_tokens(gw, upstream: FakeUpstream) -> None:
    client, _ = gw
    s = await seed(
        TENANT_A,
        routes=[RouteSpec("a.test", fmt="openai", provider="vllm")],
        caps={"tools": True, "vision": True, "context_window": 32000},
    )
    r = await client.get("/v1/models", headers={"authorization": f"Bearer {s.secret}"})
    [m] = r.json()["data"]
    assert (
        m["id"] == "m1"
        and m["capabilities"]["vision"] is True
        and m["capabilities"]["context_window"] == 32000
    )
    r = await client.get("/universal/v1/models", headers={"x-api-key": s.secret})
    assert [m["id"] for m in r.json()["data"]] == ["m1"]
    # 只有 OpenAI 格式的线路：通用接口按字符估算，Anthropic 接口不接
    r = await client.post(
        "/universal/v1/messages/count_tokens", headers={"x-api-key": s.secret}, json=_msg()
    )
    assert r.json()["estimated"] is True
    r = await client.post("/v1/messages/count_tokens", headers={"x-api-key": s.secret}, json=_msg())
    assert r.status_code == 400 and r.json()["error"]["category"] == "format_unsupported"


# ── 各格式的接口：只直通、不转换 ────────────────────────────────────────


def _openai_chunks(text: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        if line.startswith("data: ") and line[6:] != "[DONE]":
            out.append(json.loads(line[6:]))
    return out


def _openai_route() -> RouteSpec:
    return RouteSpec("v.test", provider="deepseek", fmt="openai", upstream_model="deepseek-v4-pro")


def _openai_json(text: str) -> Any:
    return lambda _: httpx.Response(
        200,
        json={
            "id": "c",
            "object": "chat.completion",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        },
    )


async def test_openai_entry_passthrough_stream(gw, upstream: FakeUpstream) -> None:
    client, runtime = gw
    s = await seed(TENANT_A, routes=[_openai_route()])
    chunks = [
        (
            None,
            {
                "id": "c",
                "model": "deepseek-v4-pro",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "你好"}}],
            },
        ),
        (
            None,
            {
                "id": "c",
                "model": "deepseek-v4-pro",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ),
        (
            None,
            {
                "id": "c",
                "model": "deepseek-v4-pro",
                "choices": [],
                "usage": {
                    "prompt_tokens": 30,
                    "prompt_cache_hit_tokens": 20,
                    "prompt_cache_miss_tokens": 10,
                    "completion_tokens": 5,
                },
            },
        ),
        (None, "[DONE]"),
    ]
    upstream.on("v.test", sse(chunks))
    body = {
        "model": "m1",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "logprobs": True,
    }
    r = await client.post(
        "/v1/chat/completions", headers={"authorization": f"Bearer {s.secret}"}, json=body
    )
    assert r.status_code == 200, r.text
    sent = upstream.hits("v.test")[0]
    assert sent.path == "/openai/chat/completions"
    # 网关不认识的字段原样带过去；要上游在最后一段带上用量
    assert sent.body["model"] == "deepseek-v4-pro" and sent.body["logprobs"] is True
    assert sent.body["stream_options"] == {"include_usage": True}
    out = _openai_chunks(r.text)
    assert {c["model"] for c in out} == {"m1"}
    # 调用方自己没要用量：网关读完用量那一段，不转给调用方
    assert all(c["choices"] for c in out) and r.text.rstrip().endswith("data: [DONE]")
    [rec] = await _records(runtime)
    assert (rec.input_tokens, rec.cache_hit_tokens, rec.output_tokens) == (10, 20, 5)
    assert rec.entry_format == "openai" and rec.attempts[0]["path"] == "direct"


async def test_openai_entry_non_stream(gw, upstream: FakeUpstream) -> None:
    client, _ = gw
    s = await seed(TENANT_A, routes=[_openai_route()])
    upstream.on("v.test", _openai_json("答案"))
    body = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}
    r = await client.post(
        "/v1/chat/completions", headers={"authorization": f"Bearer {s.secret}"}, json=body
    )
    data = r.json()
    assert data["model"] == "m1" and data["choices"][0]["message"]["content"] == "答案"
    assert data["usage"]["prompt_tokens"] == 10


async def test_format_entries_refuse_models_without_their_format(
    gw, upstream: FakeUpstream
) -> None:
    client, runtime = gw
    a = await seed(TENANT_A, model="ma", routes=[RouteSpec("a.test")])
    o = await seed(TENANT_A, model="mo", routes=[_openai_route()])
    upstream.on("a.test", anthropic_json("A"))
    upstream.on("v.test", lambda _: httpx.Response(500))
    r = await client.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {a.secret}"},
        json={"model": "ma", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 400 and r.headers["x-should-retry"] == "false"
    err = r.json()["error"]
    assert err["code"] == "format_unsupported" and "/universal/v1/messages" in err["message"]
    r = await client.post("/v1/messages", headers={"x-api-key": o.secret}, json=_msg("mo"))
    assert r.status_code == 400 and r.json()["error"]["category"] == "format_unsupported"
    # 不转换、也不试：两个上游都没被调用，线路也没被冷却
    assert upstream.hits("a.test") == [] and upstream.hits("v.test") == []
    records = await _records(runtime)
    assert [(x.entry_format, x.error_category) for x in records] == [
        ("openai", "format_unsupported"),
        ("anthropic", "format_unsupported"),
    ]


async def test_format_entry_skips_routes_of_other_formats(gw, upstream: FakeUpstream) -> None:
    client, runtime = gw
    # 首选线路是 OpenAI 格式，备用线路是 Anthropic 格式
    s = await seed(TENANT_A, routes=[_openai_route(), RouteSpec("a.test", priority=2)])
    upstream.on("a.test", anthropic_json("A"))
    upstream.on("v.test", _openai_json("O"))
    r = await client.post("/v1/messages", headers={"x-api-key": s.secret}, json=_msg())
    assert r.status_code == 200 and r.json()["content"][0]["text"] == "A"
    assert upstream.hits("v.test") == []
    # 通用接口按优先级走首选线路，转成 OpenAI 格式
    r = await client.post("/universal/v1/messages", headers={"x-api-key": s.secret}, json=_msg())
    assert r.status_code == 200 and r.json()["content"][0]["text"] == "O", r.text
    assert len(upstream.hits("v.test")) == 1 and len(upstream.hits("a.test")) == 1
    records = await _records(runtime)
    assert [(x.entry_format, x.attempts[0]["path"]) for x in records] == [
        ("anthropic", "direct"),
        ("universal", "convert:openai"),
    ]


def _gemini_chunks() -> list[tuple[None, Any]]:
    return [
        (
            None,
            {
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": "想一下", "thought": True}]}}
                ],
                "modelVersion": "gemini-2.5-flash",
            },
        ),
        (None, {"candidates": [{"content": {"role": "model", "parts": [{"text": "答案"}]}}]}),
        (
            None,
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"functionCall": {"name": "read", "args": {"path": "/a"}}}],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 50,
                    "cachedContentTokenCount": 10,
                    "candidatesTokenCount": 7,
                    "thoughtsTokenCount": 3,
                },
            },
        ),
    ]


async def test_universal_to_gemini_stream(gw, upstream: FakeUpstream) -> None:
    client, runtime = gw
    s = await seed(
        TENANT_A,
        routes=[
            RouteSpec("g.test", provider="gemini", fmt="google", upstream_model="gemini-2.5-flash")
        ],
    )
    upstream.on("g.test", sse(_gemini_chunks()))
    body = _msg(
        stream=True,
        system="你是助手",
        tools=[
            {
                "name": "read",
                "input_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"path": {"type": "string"}},
                },
            }
        ],
        thinking={"type": "enabled", "budget_tokens": 2000},
        messages=[
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "/x"}}
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "内容"}],
            },
        ],
    )
    r = await client.post("/universal/v1/messages", headers={"x-api-key": s.secret}, json=body)
    assert r.status_code == 200, r.text
    sent = upstream.hits("g.test")[0]
    assert sent.path == "/google/models/gemini-2.5-flash:streamGenerateContent"
    assert sent.headers["x-goog-api-key"] == "upstream-secret-g.test"
    assert sent.body["systemInstruction"] == {"parts": [{"text": "你是助手"}]}
    assert sent.body["contents"][2]["parts"][0]["functionResponse"]["name"] == "read"
    assert "additionalProperties" not in json.dumps(sent.body["tools"])
    events = _events(r.text)
    assert [e[1]["content_block"]["type"] for e in events if e[0] == "content_block_start"] == [
        "thinking",
        "text",
        "tool_use",
    ]
    final = next(e[1] for e in events if e[0] == "message_delta")
    assert final["delta"]["stop_reason"] == "tool_use"
    [rec] = await _records(runtime)
    assert (rec.input_tokens, rec.cache_hit_tokens, rec.output_tokens) == (40, 10, 10)
