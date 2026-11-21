"""模型管理平台在 Atlas 这一侧：调用中途失败怎么告诉用户、客户端 Key 怎么取、Run 开始的检查
（agent-platform/doc/design/model-design.html 7.3、7.5）。

不碰 DB、不起服务：模型网关的拒绝用 SDK 的真实异常类型构造（错误体照网关 Anthropic 格式入口的
error_body），平台的内部接口用 MockTransport 顶替。
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import anthropic
import httpx
import pytest
from atlas_agent.providers.llm.model_platform import (
    ClientKey,
    ModelNotAllowed,
    ModelPlatformClient,
    ModelPlatformUnavailable,
    PlatformModel,
    admit_models,
    agent_key,
    client_key,
    reset_model_platform_cache,
)
from atlas_engine.contracts.events import EventType
from atlas_engine.contracts.spec import AgentSpec, ModelSpec
from atlas_engine.runtime.failures import model_call_failure

from tests.fakes import raising_model
from tests.graphs import run_agent

RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
AGENT = "33333333-3333-3333-3333-333333333333"


def _gateway_refusal(
    cls: type[anthropic.APIStatusError], status: int, category: str, message: str
) -> anthropic.APIStatusError:
    """模型网关的拒绝：Anthropic 格式的错误体，error.category 是网关统一的错误类别。"""
    return cls(
        f"Error code: {status}",
        response=httpx.Response(status, request=httpx.Request("POST", "http://gw/v1/messages")),
        body={
            "type": "error",
            "error": {"type": _ANTHROPIC_TYPE[status], "message": message, "category": category},
        },
    )


_ANTHROPIC_TYPE = {
    401: "authentication_error",
    403: "permission_error",
    429: "rate_limit_error",
    503: "overloaded_error",
}


async def _failed(exc: Exception) -> dict[str, Any]:
    spec = AgentSpec(slug="a", name="a", system_prompt="", model=ModelSpec(model="m1"))
    events = [
        e
        async for e in run_agent(
            spec=spec, model=raising_model(exc), run_id=RUN_ID, input_content="hi"
        )
    ]
    assert events[-1].type == EventType.RUN_FAILED
    return dict(events[-1].data)


# ───────────────────────────── 调用中途失败


async def test_model_removed_from_key_stops_the_run() -> None:
    exc = _gateway_refusal(
        anthropic.PermissionDeniedError, 403, "model_forbidden", "这把 Key 不能用模型 m1"
    )
    data = await _failed(exc)
    assert data["error_kind"] == "model_not_allowed"
    assert data["retryable"] is False
    assert "不在智能体的模型 Key 里" in data["message"]
    assert data["reason"] == "model_forbidden"


async def test_key_blocked_or_reset_mid_run_tells_the_user_to_resend() -> None:
    exc = _gateway_refusal(anthropic.AuthenticationError, 401, "key_invalid", "这把 Key 已停用")
    data = await _failed(exc)
    assert data["error_kind"] == "model_unavailable"
    assert "重发" in data["message"]


def test_key_limit_is_refused() -> None:
    """Key 的预算或每分钟次数到了上限：换模型没用（同一把 Key），找管理员。"""
    exc = _gateway_refusal(anthropic.RateLimitError, 429, "key_limit", "本月的预算已经用完")
    found = model_call_failure(exc)
    assert found is not None and found.kind == "model_not_allowed"
    assert "限额" in found.message


def test_no_route_suggests_another_model_without_provider_details() -> None:
    """模型的线路都用不了（供应商余额不足、凭据失效）：提示换模型，不露供应商的原文。"""
    exc = _gateway_refusal(anthropic.APIStatusError, 503, "no_route", "这个模型暂时用不了")
    found = model_call_failure(exc)
    assert found is not None and found.kind == "model_rate_limited"
    assert "换一个模型" in found.message


def test_platform_outage_at_run_start_only_says_busy() -> None:
    """开跑前平台连不上：用户处理不了，只说系统忙。

    模型没分配给智能体则要说清楚：用户能换模型、能找管理员。
    """
    from atlas_agent.executor.runtime import _classify_assembly_error

    outage = ModelPlatformUnavailable("模型管理平台返回 HTTP 500：Internal Server Error")
    kind, message, _ = _classify_assembly_error(outage)
    assert kind == "capability_unavailable"
    assert message == "系统忙不过来了，请稍后再试"
    refused = ModelNotAllowed("智能体「a」没有分配模型 m2，请换一个已分配的模型")
    assert _classify_assembly_error(refused)[1] == str(refused)


def test_other_failures_are_left_to_engine_classify() -> None:
    """模型繁忙、上游出错：交给 engine 的 classify 按状态码分。"""
    assert model_call_failure(RuntimeError("boom")) is None
    busy = _gateway_refusal(anthropic.RateLimitError, 429, "model_busy", "模型繁忙")
    assert model_call_failure(busy) is None


# ───────────────────────────── 客户端 Key


_SETTINGS = SimpleNamespace(
    model_platform_url="http://platform",
    model_gateway_url="http://gw/universal",
    model_platform_timeout_s=5.0,
)


@pytest.fixture
def platform() -> Iterator[dict[str, Any]]:
    """平台的内部接口：客户端 Key 存在 state["keys"] 里；state["down"] = 不可达。"""
    state: dict[str, Any] = {"keys": {}, "down": False, "calls": []}

    def handle(request: httpx.Request) -> httpx.Response:
        if state["down"]:
            raise httpx.ConnectError("down", request=request)
        state["calls"].append((request.method, request.url.path))
        assert request.headers.get("x-caller-app"), "内部接口要带 X-Caller-App"
        if request.method == "POST":
            import json

            body = json.loads(request.content)
            key = state["keys"].setdefault(
                body["client_code"],
                {
                    "client_type": body["client_type"],
                    "client_code": body["client_code"],
                    "name": body["name"],
                    "status": "pending_models",
                    "key": None,
                    "models": [],
                },
            )
            return httpx.Response(200, json=key)
        code = request.url.params.get("client_code")
        if code not in state["keys"]:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json=state["keys"][code])

    reset_model_platform_cache()
    ModelPlatformClient.transport = httpx.MockTransport(handle)
    try:
        yield state
    finally:
        ModelPlatformClient.transport = None
        reset_model_platform_cache()


async def test_agent_key_is_created_pending_when_missing(platform: dict[str, Any]) -> None:
    """平台接入前建的智能体：第一次取时补建，「待分配」。"""
    key = await agent_key(_SETTINGS, AGENT, "代码评审助手")  # type: ignore[arg-type]
    assert key.status == "pending_models" and key.name == "代码评审助手"
    assert [c[0] for c in platform["calls"]] == ["GET", "POST"]
    with pytest.raises(ModelNotAllowed, match="还没有分配模型"):
        admit_models(key, ["m1"])


async def test_platform_down_reuses_the_last_key(platform: dict[str, Any]) -> None:
    platform["keys"][AGENT] = {
        "client_type": "Agent",
        "client_code": AGENT,
        "name": "代码评审助手",
        "status": "active",
        "key": "sk-1",
        "models": [{"name": "m1", "display_name": "M1", "provider": "deepseek"}],
    }
    first = await agent_key(_SETTINGS, AGENT, "代码评审助手")  # type: ignore[arg-type]
    platform["down"] = True
    again = await agent_key(_SETTINGS, AGENT, "代码评审助手")  # type: ignore[arg-type]
    assert again == first and again.key == "sk-1"
    # 从没取到过的：报暂时不可用
    with pytest.raises(ModelPlatformUnavailable):
        await client_key(_SETTINGS, "Service", "session-title")  # type: ignore[arg-type]


def test_admit_models_names_the_missing_models() -> None:
    key = ClientKey(
        client_type="Agent",
        client_code=AGENT,
        name="代码评审助手",
        status="active",
        key="sk-1",
        models=(PlatformModel(name="m1", display_name="M1", provider="deepseek"),),
    )
    admit_models(key, ["m1"])
    with pytest.raises(ModelNotAllowed, match="没有分配模型 m2"):
        admit_models(key, ["m1", "m2"])
    blocked = ClientKey(client_type="Agent", client_code=AGENT, name="x", status="blocked")
    with pytest.raises(ModelNotAllowed, match="已被停用"):
        admit_models(blocked, ["m1"])
