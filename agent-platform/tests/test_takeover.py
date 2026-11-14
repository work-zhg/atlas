"""请人接管浏览器（c4-model/agent/browser/code-browser-takeover.html）。

request_takeover 的挂起与续跑：

挂起      首次调用登记 kind=takeover、发接管卡片事件、挂起，不写工具结果
参数      地址只能是 http(s)：不合法的回工具错误让模型改，不登记
续跑      交还 → 「先重新拍快照」，附上说明、登录态没存下来的提示；不处理 → 不要再请求；作废
装配      只在开了浏览器、有沙箱时装上；没有门禁时不挂起
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from atlas_engine.contracts import ApprovalState, ExecuteResponse
from atlas_engine.contracts.events import EventType
from atlas_engine.contracts.spec import AgentSpec, LimitSpec, ModelSpec
from atlas_engine.kernel.middleware.approval import approval_id_for
from atlas_engine.kernel.middleware.takeover import (
    DECLINED,
    EXPIRED,
    HANDED_BACK,
    LOGIN_NOT_SAVED,
    NO_GATE,
    REQUEST_TAKEOVER,
    takeover_result,
)
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from tests.graphs import run_agent as run

from .fakes import TurnModel, tool_call_chunk

RUN_ID = UUID("66666666-6666-6666-6666-666666666666")
TOKEN = approval_id_for(str(RUN_ID), "c1")
ASK = {
    "reason": "淘宝要登录才能查订单，请在画面里登录，登录完点交还。",
    "url": "https://login.taobao.com/",
}


class ScriptedGate:
    def __init__(self, state: ApprovalState = "pending") -> None:
        self.state = state
        self.asked: list[dict[str, Any]] = []

    async def check(self, *, approval_id: str, **extra: Any) -> ApprovalState:
        self.asked.append({"approval_id": approval_id, **extra})
        return self.state


class Shell:
    id = "sbx:test"

    def execute(self, command: str, **kw: Any) -> ExecuteResponse:
        raise NotImplementedError

    async def aexecute(self, command: str, **kw: Any) -> ExecuteResponse:
        return ExecuteResponse(output="", exit_code=0)


def _spec(*, browser: bool = True) -> AgentSpec:
    return AgentSpec(
        slug="shopper",
        name="购物助理",
        system_prompt="p",
        model=ModelSpec(model="claude-sonnet-5"),
        tool_names=("bash",),
        limits=LimitSpec(timeout_s=20),
        browser=browser,
    )


def _model(args: dict[str, Any] | None = None) -> TurnModel:
    payload = json.dumps(ASK if args is None else args, ensure_ascii=False)
    return TurnModel(
        scripts=[[tool_call_chunk(REQUEST_TAKEOVER, payload, "c1")], [AIMessageChunk(content="好")]]
    )


async def _collect(gate: Any, *, model: Any = None, spec: AgentSpec | None = None) -> list:
    return [
        e
        async for e in run(
            spec or _spec(),
            run_id=RUN_ID,
            model=model or _model(),
            input_content="帮我查一下淘宝上周的订单",
            approvals=gate,
            sandbox=Shell(),
        )
    ]


async def _resume(outcome: dict[str, Any]) -> tuple[list, TurnModel]:
    model = TurnModel(scripts=[[AIMessageChunk(content="接着做")]])
    history = [
        HumanMessage(content="帮我查一下淘宝上周的订单"),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": REQUEST_TAKEOVER, "args": ASK}]),
    ]
    events = [
        e
        async for e in run(
            _spec(),
            run_id=RUN_ID,
            model=model,
            input_content=None,
            history=history,
            resume=True,
            approvals=ScriptedGate("pending"),
            sandbox=Shell(),
            waiting_on={"c1": {"reason": "approval", "token": TOKEN}},
            decisions={TOKEN: outcome},
        )
    ]
    return events, model


def _tool_result(model: TurnModel) -> ToolMessage:
    seen = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert len(seen) == 1, seen
    return seen[0]


# ---------------------------------------------------------------- 挂起


async def test_request_suspends_and_registers_a_takeover() -> None:
    gate = ScriptedGate("pending")
    events = await _collect(gate)
    asked = gate.asked[0]
    assert asked["approval_id"] == TOKEN
    assert (asked["kind"], asked["source"]) == ("takeover", "browser")
    assert asked["args"] == ASK and asked["reason"] == ASK["reason"]
    required = next(e for e in events if e.type is EventType.APPROVAL_REQUIRED)
    assert required.data["approval_kind"] == "takeover"
    assert required.data["args"]["url"] == "https://login.taobao.com/"
    assert required.data["tool_call_id"] == "c1"
    suspended = next(e for e in events if e.type is EventType.RUN_SUSPENDED)
    assert suspended.data["waiting_on"] == [
        {"tool_call_id": "c1", "reason": "approval", "token": TOKEN}
    ]
    types = [e.type for e in events]
    assert EventType.TOOL_COMPLETED not in types and EventType.TOOL_FAILED not in types


async def test_non_http_url_goes_back_to_the_model() -> None:
    gate = ScriptedGate("pending")
    events = await _collect(gate, model=_model({"reason": "登录", "url": "file:///etc/passwd"}))
    assert gate.asked == []
    failed = [e for e in events if e.type is EventType.TOOL_FAILED]
    assert [e.data["call_id"] for e in failed] == ["c1"]
    assert events[-1].type is EventType.RUN_FINISHED


async def test_without_gate_it_does_not_suspend() -> None:
    model = _model()
    events = [
        e
        async for e in run(_spec(), run_id=RUN_ID, model=model, input_content="x", sandbox=Shell())
    ]
    assert events[-1].type is EventType.RUN_FINISHED
    assert _tool_result(model).content == NO_GATE


async def test_tool_only_with_browser_and_sandbox() -> None:
    """没开浏览器、或没有沙箱：模型看不到 request_takeover（调了也是未知工具）。"""
    for spec, sandbox in ((_spec(browser=False), Shell()), (_spec(), None)):
        model = _model()
        gate = ScriptedGate("pending")
        events = [
            e
            async for e in run(
                spec, run_id=RUN_ID, model=model, input_content="x", approvals=gate, sandbox=sandbox
            )
        ]
        assert gate.asked == []
        # 未知工具：工具节点回错误，没有登记、没有挂起
        assert EventType.APPROVAL_REQUIRED not in [e.type for e in events]
        assert events[-1].type is EventType.RUN_FINISHED


# ---------------------------------------------------------------- 续跑


async def test_handed_back_tells_the_model_to_snapshot_again() -> None:
    events, model = await _resume(
        {
            "status": "approved",
            "feedback": "登录好了",
            "login": {"saved": True, "sites": ["taobao.com"]},
        }
    )
    result = _tool_result(model)
    assert result.content.startswith(HANDED_BACK)
    assert "「登录好了」" in result.content and LOGIN_NOT_SAVED not in result.content
    settled = next(e for e in events if e.type is EventType.TAKEOVER_SETTLED)
    assert settled.data["decision"] == "approved"
    assert settled.data["login"] == {"saved": True, "sites": ["taobao.com"]}
    assert events[-1].type is EventType.RUN_FINISHED


async def test_handed_back_without_saving_says_so() -> None:
    _, model = await _resume({"status": "approved", "login": {"saved": False, "sites": []}})
    assert LOGIN_NOT_SAVED in _tool_result(model).content


async def test_declined_and_expired() -> None:
    _, model = await _resume({"status": "rejected", "feedback": "不想登录"})
    result = _tool_result(model)
    assert result.content == DECLINED.format(why="，原因：「不想登录」")
    assert result.status == "success"
    _, model = await _resume({"status": "expired"})
    assert _tool_result(model).content == EXPIRED


def test_result_text_for_gate_failure_is_an_error() -> None:
    text, error = takeover_result({"status": "failed"})
    assert error and "故障" in text
    assert takeover_result("approved") == (HANDED_BACK, False)
