"""网关里不碰数据库、不连网络的部分：供应商方言、能力与参数、线路、读写器、转换器。"""

from __future__ import annotations

import json
import uuid
from collections import Counter

import pytest
from atlas_modelgateway.gateway.capability import CapabilityGate, ParamPolicy
from atlas_modelgateway.gateway.format.codecs import AnthropicCodec, OutContext, wrap_signature
from atlas_modelgateway.gateway.format.convert import AnthropicToOpenAI
from atlas_modelgateway.gateway.format.sse import SSEEvent
from atlas_modelgateway.gateway.kernel import (
    AccountRef,
    FailureKind,
    Format,
    GatewayError,
    Intent,
    ModelCaps,
    ModelKind,
    ParamPlan,
    RequestSummary,
    Route,
    ThinkingSupport,
    UpstreamFailure,
)
from atlas_modelgateway.gateway.presets import PresetRules
from atlas_modelgateway.gateway.routing import (
    AccountIssue,
    FailurePolicy,
    RouteHealth,
    RouteSelector,
    StickyPlacement,
)
from atlas_modelgateway.provider import ProviderCatalog, join_url

CATALOG = ProviderCatalog()


def _route(provider: str = "anthropic", upstream: str = "claude-opus-5", **kw) -> Route:
    account = AccountRef(
        uuid=kw.pop("account_uuid", uuid.uuid4()),
        provider=provider,
        endpoints=kw.pop("endpoints", {Format.ANTHROPIC: "https://api.example.com"}),
        secret="sk-test",
    )
    return Route(uuid=uuid.uuid4(), model="m", account=account, upstream_model=upstream, **kw)


# ── 供应商方言 ─────────────────────────────────────────────────────────


def test_url_keeps_query_and_fills_model() -> None:
    assert (
        join_url(
            "https://r.openai.azure.com/openai/deployments/{model}?api-version=2024-10-21",
            "/chat/completions",
            "gpt",
        )
        == "https://r.openai.azure.com/openai/deployments/gpt/chat/completions?api-version=2024-10-21"
    )


@pytest.mark.parametrize(
    ("provider", "status", "body", "kind"),
    [
        (
            "openai",
            429,
            {"error": {"type": "insufficient_quota", "code": "insufficient_quota"}},
            FailureKind.NO_BALANCE,
        ),
        ("deepseek", 429, {"error": {"type": "rate_limit"}}, FailureKind.RATE_LIMITED),
        (
            "anthropic",
            529,
            {"type": "error", "error": {"type": "overloaded_error"}},
            FailureKind.OVERLOADED,
        ),
        ("deepseek", 402, {"error": {"message": "Insufficient Balance"}}, FailureKind.NO_BALANCE),
        (
            "anthropic",
            401,
            {"type": "error", "error": {"type": "authentication_error"}},
            FailureKind.AUTH_FAILED,
        ),
        (
            "anthropic",
            400,
            {"type": "error", "error": {"type": "invalid_request_error"}},
            FailureKind.BAD_REQUEST,
        ),
        (
            "anthropic",
            None,
            {"type": "error", "error": {"type": "overloaded_error"}},
            FailureKind.OVERLOADED,
        ),
    ],
)
def test_classify(provider: str, status: int | None, body: dict, kind: FailureKind) -> None:
    raw = json.dumps(body).encode()
    failure = CATALOG.dialect(provider).classify(
        status, {"retry-after": "7"}, raw if status else body
    )
    assert failure.kind is kind
    if status:
        assert failure.retry_after_s == 7
    assert (failure.body is not None) is (kind is FailureKind.BAD_REQUEST)


def test_deepseek_usage_reads_its_own_cache_fields() -> None:
    raw = {
        "prompt_tokens": 50_000,
        "prompt_cache_hit_tokens": 48_000,
        "prompt_cache_miss_tokens": 2_000,
        "completion_tokens": 800,
    }
    usage = CATALOG.dialect("deepseek").usage(Format.OPENAI, raw)
    assert (usage.input, usage.cache_hit, usage.output) == (2_000, 48_000, 800)
    # 按标准字段读就会读成没命中
    standard = CATALOG.dialect("custom_openai").usage(Format.OPENAI, raw)
    assert (standard.input, standard.cache_hit) == (50_000, 0)


def test_qwen_thinking_field_name() -> None:
    plan = ParamPlan(upstream_model="qwen3-max", thinking="off")
    assert CATALOG.dialect("dashscope").adjust(Format.OPENAI, {}, plan) == {
        "enable_thinking": False
    }
    plan = ParamPlan(upstream_model="qwen3-max", thinking="budget", thinking_budget=4000)
    assert CATALOG.dialect("dashscope").adjust(Format.OPENAI, {}, plan) == {
        "enable_thinking": True,
        "thinking_budget": 4000,
    }


def test_auth_styles() -> None:
    assert CATALOG.dialect("anthropic").auth(Format.ANTHROPIC, "k")["x-api-key"] == "k"
    assert CATALOG.dialect("deepseek").auth(Format.ANTHROPIC, "k") == {"Authorization": "Bearer k"}
    assert CATALOG.dialect("azure").auth(Format.OPENAI, "k") == {"api-key": "k"}
    assert CATALOG.dialect("vllm").auth(Format.OPENAI, "") == {}


# ── 能力与参数 ─────────────────────────────────────────────────────────

PRESETS = PresetRules()
CAPS = ModelCaps(tools=True, vision=True, thinking=ThinkingSupport.OPTIONAL, max_output=128_000)


def _plan(intent: Intent, provider: str, upstream: str, caps: ModelCaps = CAPS) -> ParamPlan:
    route = _route(provider, upstream)
    return ParamPolicy.decide(intent, PRESETS.rules_for(provider, upstream), caps, route)


def test_opus_drops_temperature_and_keeps_adaptive() -> None:
    plan = _plan(
        Intent(thinking="auto", temperature=0.2, max_output=64_000), "anthropic", "claude-opus-5"
    )
    assert plan.temperature is None and "top_p" in plan.drop_fields
    assert plan.thinking == "adaptive"
    assert plan.max_output == 64_000


def test_haiku_has_only_budget_thinking() -> None:
    plan = _plan(Intent(thinking="auto", max_output=8_000), "anthropic", "claude-haiku-4-5")
    assert plan.thinking == "budget" and plan.thinking_budget == 7_999


def test_off_lowers_effort_and_minimax_never_gets_thinking_param() -> None:
    plan = _plan(Intent(thinking="off", effort="max"), "anthropic", "claude-opus-5")
    assert (plan.thinking, plan.effort) == ("off", "high")
    plan = _plan(
        Intent(thinking="off", max_output=64_000),
        "minimax",
        "MiniMax-M3",
        ModelCaps(max_output=32_768),
    )
    assert plan.thinking is None
    assert plan.max_output == 32_768


def test_unknown_upstream_is_conservative() -> None:
    plan = _plan(Intent(thinking="on", effort="high", temperature=0.5), "vllm", "my-model")
    assert (plan.thinking, plan.effort, plan.temperature) == (None, None, None)
    assert plan.strip_history_thinking


def test_capability_rejects_images_for_text_only_model() -> None:
    gate = CapabilityGate(PRESETS)
    summary = RequestSummary(model="m", has_images=True)
    with pytest.raises(GatewayError) as err:
        gate.check("m", ModelCaps(tools=True), summary)
    assert "不能看图" in err.value.message
    with pytest.raises(GatewayError):
        gate.check("m", ModelCaps(kind=ModelKind.EMBEDDING), RequestSummary(model="m"))


# ── 线路 ───────────────────────────────────────────────────────────────


def test_sticky_placement_is_stable_and_weighted() -> None:
    a, b = _route(weight=3), _route(weight=1)
    assert StickyPlacement.rank("s1", [a, b])[0] == StickyPlacement.rank("s1", [b, a])[0]
    wins = Counter(StickyPlacement.rank(f"s{i}", [a, b])[0].uuid for i in range(4000))
    assert 0.68 < wins[a.uuid] / 4000 < 0.82


async def test_pick_skips_cooling_and_abnormal_then_falls_back() -> None:
    health = RouteHealth(None)
    selector = RouteSelector(health)
    a, b = _route(priority=1), _route(priority=1)
    c = _route(priority=2)
    routes = [a, b, c]
    first = await selector.pick(routes, "s")
    assert first in (a, b)
    other = b if first is a else a
    await health.cool(first.uuid, 60)
    assert await selector.pick(routes, "s") is other
    await health.mark(other.account.uuid, AccountIssue.NO_BALANCE)
    assert await selector.pick(routes, "s") is c
    await health.cool(c.uuid, 30)
    # 全部不可用：冷却最早结束的那条
    assert await selector.pick(routes, "s") is c
    assert await selector.pick([other], "s") is None


def test_failure_policy() -> None:
    f = lambda kind, **kw: UpstreamFailure(kind, "x", **kw)  # noqa: E731
    assert not FailurePolicy.verdict(f(FailureKind.BAD_REQUEST)).switch
    assert FailurePolicy.verdict(f(FailureKind.RATE_LIMITED, retry_after_s=5)).cool_route_s == 5
    assert FailurePolicy.verdict(f(FailureKind.NO_BALANCE)).account_issue is AccountIssue.NO_BALANCE


# ── 读写器 ─────────────────────────────────────────────────────────────

CODEC = AnthropicCodec()


def test_anthropic_summarize() -> None:
    body = {
        "model": "deepseek-v4-pro",
        "max_tokens": 64000,
        "stream": True,
        "thinking": {"type": "adaptive"},
        "temperature": 0.2,
        "tools": [{"name": "read"}],
        "messages": [
            {
                "role": "user",
                "content": [{"type": "image", "source": {"type": "base64", "data": "AAAA" * 1000}}],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "…", "signature": wrap_signature("abc", "sig")}
                ],
            },
        ],
    }
    s = CODEC.summarize(body, {"X-Session-Id": "t1"}, ModelKind.CHAT)
    assert (s.model, s.stream, s.session_key, s.has_images, s.has_tools) == (
        "deepseek-v4-pro",
        True,
        "t1",
        True,
        True,
    )
    assert s.intent == Intent(thinking="auto", temperature=0.2, max_output=64000)
    assert s.thinking_stamps == {"abc"}
    assert s.approx_input_tokens < 50  # base64 不算


def test_anthropic_apply_history_by_stamp() -> None:
    body = {
        "model": "m",
        "max_tokens": 100,
        "temperature": 0.3,
        "top_p": 0.9,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "a",
                        "signature": wrap_signature("same", "orig"),
                    },
                    {
                        "type": "thinking",
                        "thinking": "b",
                        "signature": wrap_signature("other", "x"),
                    },
                    {"type": "text", "text": "hi"},
                ],
            },
        ],
    }
    plan = ParamPlan(
        upstream_model="up", thinking="off", drop_fields=frozenset({"top_p"}), stamp="same"
    )
    out = CODEC.apply(body, plan)
    assert out["model"] == "up" and out["thinking"] == {"type": "disabled"}
    assert "temperature" not in out and "top_p" not in out
    blocks = out["messages"][0]["content"]
    assert [b.get("signature") for b in blocks if b["type"] == "thinking"] == ["orig"]
    assert body["messages"][0]["content"][0]["signature"].startswith("atlas1:")  # 原请求没被改


def test_anthropic_outgoing_wraps_signature_and_restores_model() -> None:
    ctx = OutContext(model="deepseek-v4-pro", stamp="st")
    start = SSEEvent.of(
        {"type": "message_start", "message": {"model": "up-model", "usage": {"input_tokens": 3}}},
        "message_start",
    )
    assert CODEC.outgoing(start, ctx).json()["message"]["model"] == "deepseek-v4-pro"
    sig = SSEEvent.of(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "S"},
        },
        "content_block_delta",
    )
    assert CODEC.outgoing(sig, ctx).json()["delta"]["signature"] == "atlas1:st:S"
    assert CODEC.inspect(start, ctx).usage == {"input_tokens": 3}


# ── 转换器 ─────────────────────────────────────────────────────────────

A2O = AnthropicToOpenAI(CATALOG.dialect("deepseek"))


def test_anthropic_to_openai_request() -> None:
    body = {
        "model": "up",
        "max_tokens": 100,
        "stream": True,
        "system": [{"type": "text", "text": "你是助手", "cache_control": {"type": "ephemeral"}}],
        "tools": [{"name": "read", "description": "读文件", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "any"},
        "messages": [
            {"role": "user", "content": "读一下"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "…", "signature": "s"},
                    {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "a"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "内容"}],
                    },
                    {"type": "text", "text": "继续"},
                ],
            },
        ],
    }
    out, dropped = A2O.request(body)
    assert out["messages"][0] == {"role": "system", "content": "你是助手"}
    assert out["messages"][2]["tool_calls"][0]["function"] == {
        "name": "read",
        "arguments": '{"path": "a"}',
    }
    assert out["messages"][3] == {"role": "tool", "tool_call_id": "t1", "content": "内容"}
    assert out["messages"][4] == {"role": "user", "content": "继续"}
    assert out["tool_choice"] == "required" and out["stream_options"] == {"include_usage": True}
    assert {"cache_control", "thinking"} <= set(dropped)


def test_openai_stream_to_anthropic_events() -> None:
    t = A2O.transcoder()
    chunks = [
        {
            "id": "c1",
            "model": "up",
            "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "想"}}],
        },
        {"choices": [{"index": 0, "delta": {"content": "你"}}]},
        {"choices": [{"index": 0, "delta": {"content": "好"}}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "read", "arguments": '{"pa'},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": 'th": "/a"}'}}]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_2",
                                "function": {"name": "x", "arguments": "{bad"},
                            }
                        ]
                    },
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 12_000,
                "prompt_cache_hit_tokens": 10_000,
                "prompt_cache_miss_tokens": 2_000,
                "completion_tokens": 300,
            },
        },
    ]
    events = []
    for c in chunks:
        events += t.feed(SSEEvent.of(c))
    events += t.feed(SSEEvent(None, "[DONE]"))
    names = [e.event for e in events]
    assert names[0] == "message_start" and names[-2:] == ["message_delta", "message_stop"]
    starts = [e.json()["content_block"]["type"] for e in events if e.event == "content_block_start"]
    assert starts == ["thinking", "text", "tool_use", "tool_use"]
    inputs = [
        e.json()["delta"]["partial_json"]
        for e in events
        if e.event == "content_block_delta" and e.json()["delta"]["type"] == "input_json_delta"
    ]
    assert json.loads(inputs[0]) == {"path": "/a"}
    assert json.loads(inputs[1]) == {"_raw_arguments": "{bad"}
    final = next(e.json() for e in events if e.event == "message_delta")
    assert final["delta"]["stop_reason"] == "tool_use"
    assert (
        final["usage"]["input_tokens"] == 2_000
        and final["usage"]["cache_read_input_tokens"] == 10_000
    )
    # 每个块都关上了
    assert names.count("content_block_start") == names.count("content_block_stop") == 4
