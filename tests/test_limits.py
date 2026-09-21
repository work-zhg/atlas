"""P6 · 运行限制全量生效 + 错误分类（文档 §4.4 / §13）。

这些控件在编辑器里一直是可填的，但直到 P6 才真正生效。
「配了不生效」比「没有这个选项」更糟 —— 用户以为设了上限，实际没有。
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from atlas_engine.contracts import (
    EngineError,
    LimitExceeded,
    ModelRateLimited,
    ModelRefused,
    ModelUnavailable,
    classify,
)
from atlas_server.domain.events import EventType
from atlas_engine.kernel.middleware.limits import (
    CIRCUIT_THRESHOLD,
    StepLimitMiddleware,
    ToolGovernorMiddleware,
)
from tests.graphs import run_agent as run
from atlas_server.domain.spec import AgentSpec, LimitSpec, ModelSpec, SubAgentSpec
from langchain_core.messages import AIMessageChunk, ToolMessage

from .fakes import TurnModel, raising_model, text_model, tool_call_chunk

RUN_ID = UUID("99999999-9999-9999-9999-999999999999")


def _spec(**limits: Any) -> AgentSpec:
    return AgentSpec(
        slug="bounded",
        name="受限",
        system_prompt="p",
        model=ModelSpec(model="claude-sonnet-5"),
        tool_names=("write_todos",),
        limits=LimitSpec(timeout_s=30, **limits),
    )


def _looping_model() -> TurnModel:
    """永远只发工具调用、从不给结论 —— 模拟跑飞的 agent。"""
    return TurnModel(
        scripts=[[tool_call_chunk("write_todos", '{"todos":[]}', f"c{i}")] for i in range(50)]
    )


# ---------------------------------------------------------------- max_steps


async def test_step_limit_stops_a_runaway_agent() -> None:
    """★ 步数是跑飞的 agent 唯一可靠的刹车。

    token 上限只能在调用返回后检查（§4.4）；一个陷在「调工具→再调」
    循环里的 agent 每步 token 不多，会一直转到超时。
    """
    events = [
        e
        async for e in run(
            _spec(max_steps=3),
            run_id=RUN_ID,
            model=_looping_model(),
            input_content="开始",
        )
    ]
    failed = next(e for e in events if e.type is EventType.RUN_FAILED)
    assert failed.data["error_kind"] == LimitExceeded.kind
    assert failed.data["max_steps"] == 3


async def test_step_limit_not_hit_on_normal_run() -> None:
    events = [
        e
        async for e in run(
            _spec(max_steps=40),
            run_id=RUN_ID,
            model=text_model("直接回答"),
            input_content="你好",
        )
    ]
    assert events[-1].type is EventType.RUN_FINISHED


async def test_step_middleware_counts_model_calls() -> None:
    middleware = StepLimitMiddleware(2)
    await middleware.abefore_model({})
    await middleware.abefore_model({})
    assert middleware.steps_used == 2
    with pytest.raises(LimitExceeded):
        await middleware.abefore_model({})


# ---------------------------------------------------------------- 并发


async def test_tool_concurrency_is_capped() -> None:
    """★ 模型一次可以发多个 tool_call，图会并发执行。

    不设闸的话 10 个并发 SQL 能把数据库打满 —— 这正是这个配置项存在的理由。
    """
    governor = ToolGovernorMiddleware(concurrency=2)
    peak = 0
    current = 0

    async def handler(_request: Any) -> ToolMessage:
        nonlocal peak, current
        current += 1
        peak = max(peak, current)
        await asyncio.sleep(0.05)
        current -= 1
        return ToolMessage(content="ok", tool_call_id="x", name="t")

    request = type("R", (), {"tool_call": {"name": "t", "id": "x"}})()
    await asyncio.gather(*(governor.awrap_tool_call(request, handler) for _ in range(8)))

    assert peak <= 2, f"并发峰值 {peak}，超过上限 2"


# ---------------------------------------------------------------- 熔断


async def _run_governor(governor: ToolGovernorMiddleware, *, fail: bool, times: int) -> list[Any]:
    async def handler(_request: Any) -> ToolMessage:
        return ToolMessage(
            content="炸了" if fail else "ok",
            tool_call_id="x",
            name="flaky",
            status="error" if fail else "success",
        )

    request = type("R", (), {"tool_call": {"name": "flaky", "id": "x"}})()
    return [await governor.awrap_tool_call(request, handler) for _ in range(times)]


async def test_circuit_opens_after_consecutive_failures() -> None:
    """§13.2：同一工具连续失败 3 次即熔断，继续试只是烧钱。"""
    governor = ToolGovernorMiddleware(concurrency=4)
    results = await _run_governor(governor, fail=True, times=CIRCUIT_THRESHOLD + 1)

    assert governor.failures_of("flaky") == CIRCUIT_THRESHOLD
    assert "已被停用" in str(results[-1].content), "第 4 次没有被熔断拦下"


async def test_circuit_does_not_kill_the_run() -> None:
    """★ 熔断也是把结果回给 agent，不是终止 run（§13.2）。

    agent 收到「该工具不可用」后可以换方案继续，而不是整轮白跑。
    """
    governor = ToolGovernorMiddleware(concurrency=4)
    results = await _run_governor(governor, fail=True, times=CIRCUIT_THRESHOLD + 1)
    assert all(isinstance(r, ToolMessage) for r in results), "熔断抛异常了"


async def test_success_resets_the_counter() -> None:
    """熔断针对「一直不通」，不是「偶尔抖动」。"""
    governor = ToolGovernorMiddleware(concurrency=4)
    await _run_governor(governor, fail=True, times=2)
    assert governor.failures_of("flaky") == 2

    await _run_governor(governor, fail=False, times=1)
    assert governor.failures_of("flaky") == 0, "成功一次后计数没清零"


# ---------------------------------------------------------------- 子智能体深度


_SUB = SubAgentSpec(
    name="r", description="d", system_prompt="s", model=ModelSpec(model="claude-sonnet-5")
)


def _delegating_spec(*, subagents: tuple[SubAgentSpec, ...], depth: int = 2) -> AgentSpec:
    return AgentSpec(
        slug="s",
        name="n",
        system_prompt="p",
        model=ModelSpec(model="claude-sonnet-5"),
        tool_names=("task",),
        subagents=subagents,
        limits=LimitSpec(max_subagent_depth=depth, timeout_s=20),
    )


class _ReplyGateway:
    """DelegationProtocol 的最小实现 —— 固定回一句话。"""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def delegate(self, task: str, name: str, *, fresh: bool = False) -> str:
        return self._reply


async def _delegate(spec: AgentSpec, target: str) -> str:
    """让模型调一次 task，返回它拿到的工具结果。

    ★ 用行为断言而不是查图结构：depth=0 与 depth=2 编译出的节点名完全相同，
      静态检查区分不了，写出来会是个永远通过的空断言。
    """
    model = TurnModel(
        scripts=[
            [tool_call_chunk("task", f'{{"description":"x","subagent_type":"{target}"}}', "c1")],
            [AIMessageChunk(content="完")],
        ]
    )
    events = [
        e
        async for e in run(
            spec,
            run_id=RUN_ID,
            model=model,
            input_content="去",
            subagents=_ReplyGateway("子结论"),
        )
    ]
    for event in events:
        if event.type in (EventType.SUBAGENT_FINISHED, EventType.TOOL_FAILED):
            return str(event.data.get("result", ""))
    return ""


async def test_delegation_works_when_configured() -> None:
    assert "子结论" in await _delegate(_delegating_spec(subagents=(_SUB,)), "r")


async def test_depth_zero_blocks_delegation() -> None:
    """max_subagent_depth=0 = 不允许委派。"""
    result = await _delegate(_delegating_spec(subagents=(_SUB,), depth=0), "r")
    assert "not a valid tool" in result, f"depth=0 没能挡住委派：{result[:80]}"


async def test_task_unavailable_without_subagents() -> None:
    """★ 没配子智能体时 `task` 根本不该存在。

    kernel 原本无条件注入一个 general-purpose 子智能体，于是即使调用方
    一个都没配，agent 也能委派 —— 用户没开这个能力，token 消耗也不受
    max_subagent_depth 约束。这与 unsupported_tools() 的契约直接冲突
    （它明确把「没配子智能体时的 task」报为不可用）。
    kernel 是自有代码，已在 graph.py 改为「调用方给了才补默认的」。
    """
    spec = AgentSpec(
        slug="s",
        name="n",
        system_prompt="p",
        model=ModelSpec(model="claude-sonnet-5"),
        tool_names=(),
        subagents=(),
        limits=LimitSpec(timeout_s=20),
    )
    result = await _delegate(spec, "general-purpose")
    assert "not a valid tool" in result, f"没配子智能体却能委派：{result[:80]}"


# ---------------------------------------------------------------- 错误分类


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, ModelRateLimited),
        (500, ModelUnavailable),
        (503, ModelUnavailable),
        (401, ModelUnavailable),
        (400, ModelUnavailable),
    ],
)
def test_classify_by_status_code(status: int, expected: type[EngineError]) -> None:
    """★ 依据是实例上的 status_code（实测 anthropic SDK 异常都带它），
    不是类名匹配 —— 类名会随 SDK 版本变，状态码是协议的一部分。"""
    exc = type("Boom", (Exception,), {})()
    exc.status_code = status  # type: ignore[attr-defined]
    assert isinstance(classify(exc), expected)


def test_classify_preserves_original_message() -> None:
    """401 最常见的原因是 key 失效，必须把原文透出来让人能去改配置。"""
    exc = type("Boom", (Exception,), {})("Invalid proxy server token")
    exc.status_code = 401  # type: ignore[attr-defined]
    assert "Invalid proxy server token" in classify(exc).message


def test_classify_passes_engine_errors_through() -> None:
    original = LimitExceeded("已超限")
    assert classify(original) is original


def test_classify_handles_missing_status_code() -> None:
    """超时与连接失败没有状态码。"""
    assert isinstance(classify(TimeoutError("超时了")), ModelUnavailable)


async def test_run_failed_carries_kind_and_retryable() -> None:
    """★ 429 走完整条链路后，事件里要既有正确的 kind 也有 retryable。

    前端靠 kind 决定文案、靠 retryable 决定显不显示「重试」按钮。
    全都归成 model_unavailable 的话，用户看到限流会以为服务挂了。
    """
    exc = type("Boom", (Exception,), {})("Rate limit exceeded")
    exc.status_code = 429  # type: ignore[attr-defined]

    events = [
        e
        async for e in run(
            _spec(),
            run_id=RUN_ID,
            model=raising_model(exc),
            input_content="x",
        )
    ]

    failed = next(e for e in events if e.type is EventType.RUN_FAILED)
    assert failed.data["error_kind"] == ModelRateLimited.kind
    assert failed.data["retryable"] is True
    assert failed.data["status_code"] == 429
    assert "Rate limit exceeded" in failed.data["message"]


async def test_refusal_becomes_model_refused() -> None:
    """§13.1：模型拒答不是故障，重试无用，原样展示它的说明。"""
    model = TurnModel(
        scripts=[
            [
                AIMessageChunk(
                    content="我不能协助这个请求。",
                    response_metadata={"stop_reason": "refusal"},
                )
            ]
        ]
    )
    events = [e async for e in run(_spec(), run_id=RUN_ID, model=model, input_content="x")]
    failed = next(e for e in events if e.type is EventType.RUN_FAILED)
    assert failed.data["error_kind"] == ModelRefused.kind
    assert failed.data["retryable"] is False
    assert "不能协助" in failed.data["message"]
