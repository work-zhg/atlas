"""模型管理平台在 Atlas 这一侧：调用中途失败怎么告诉用户、客户端 Key 怎么取、Run 开始的检查
（agent-platform/doc/design/model-design.html 7.3、7.5）。

不碰 DB、不起服务：LiteLLM 的拒绝用 SDK 的真实异常类型构造（错误体照 LiteLLM 1.106 实测），
平台的内部接口用 MockTransport 顶替。
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import anthropic
import httpx
import pytest
from atlas_agent.domain.events import EventType
from atlas_agent.domain.spec import AgentSpec, ModelSpec
from atlas_agent.providers.llm.failures import model_call_failure
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

from tests.fakes import raising_model
from tests.graphs import run_agent

RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
AGENT = "33333333-3333-3333-3333-333333333333"


def _litellm_refusal(
    cls: type[anthropic.APIStatusError], status: int, error_type: str, message: str
) -> anthropic.APIStatusError:
    """LiteLLM 的拒绝（/v1/messages 上也是 {"error": {"type": …}}）。"""
    return cls(
        f"Error code: {status}",
        response=httpx.Response(
            status, request=httpx.Request("POST", "http://litellm/v1/messages")
        ),
        body={"error": {"message": message, "type": error_type, "param": "None", "code": str(status)}},
    )


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
    exc = _litellm_refusal(
        anthropic.PermissionDeniedError,
        403,
        "key_model_access_denied",
        "The requested model 'm1' is not available for this API key",
    )
    data = await _failed(exc)
    assert data["error_kind"] == "model_not_allowed"
    assert data["retryable"] is False
    assert "不在智能体的模型 Key 里" in data["message"]
    assert data["reason"] == "key_model_access_denied"


@pytest.mark.parametrize("error_type", ["auth_error", "token_not_found_in_db", "expired_key"])
async def test_key_blocked_or_reset_mid_run_tells_the_user_to_resend(error_type: str) -> None:
    exc = _litellm_refusal(anthropic.AuthenticationError, 401, error_type, "Authentication Error")
    data = await _failed(exc)
    assert data["error_kind"] == "model_unavailable"
    assert "重发" in data["message"]


def test_budget_exceeded_is_refused() -> None:
    exc = _litellm_refusal(anthropic.BadRequestError, 400, "budget_exceeded", "Budget has been exceeded")
    found = model_call_failure(exc)
    assert found is not None and found.kind == "model_not_allowed"
    assert "预算" in found.message


def test_provider_rejections_do_not_leak_provider_details() -> None:
    """供应商凭据失效（LiteLLM 按 Anthropic 格式转回 authentication_error）、余额不足（402）。"""
    bad_credential = anthropic.AuthenticationError(
        "Error code: 401",
        response=httpx.Response(401, request=httpx.Request("POST", "http://litellm/v1/messages")),
        body={
            "type": "error",
            "error": {
                "type": "authentication_error",
                "message": "DeepseekException - Authentication Fails, Your api key: ****ider",
            },
        },
    )
    out_of_credit = anthropic.APIStatusError(
        "Error code: 402",
        response=httpx.Response(402, request=httpx.Request("POST", "http://litellm/v1/messages")),
        body={"error": {"message": "Insufficient Balance"}},
    )
    for exc in (bad_credential, out_of_credit):
        found = model_call_failure(exc)
        assert found is not None and found.kind == "model_unavailable"
        assert "Deepseek" not in found.message and "Insufficient" not in found.message


def test_other_failures_are_left_to_engine_classify() -> None:
    assert model_call_failure(RuntimeError("boom")) is None
    exc = anthropic.RateLimitError(
        "Error code: 429",
        response=httpx.Response(429, request=httpx.Request("POST", "http://litellm/v1/messages")),
        body={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
    )
    assert model_call_failure(exc) is None


# ───────────────────────────── 客户端 Key


_SETTINGS = SimpleNamespace(
    model_platform_url="http://platform",
    model_litellm_url="http://litellm",
    model_platform_timeout_s=5.0,
    litellm_base_url="http://unused",
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
