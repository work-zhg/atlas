"""P7 · 高风险工具的人工确认（文档 §12.2）。

★ 最关键的一条是 test_event_arrives_before_gate_blocks：
  审批事件必须在 gate 阻塞**之前**送达消费者。做不到的话就是死锁 ——
  用户看不到确认弹窗，界面永远卡住，而后端在等一个永远不会来的决策。
  那个测试刻意构造成「事件没先到就必然超时」，所以它挂了就是真挂了。
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from atlas_engine.contracts import Decision
from atlas_engine.kernel.middleware.approval import EXPIRED_RESULT, REJECTED_RESULT
from atlas_server.domain.events import EventType
from tests.graphs import run_agent as run
from atlas_server.domain.spec import AgentSpec, LimitSpec, ModelSpec
from langchain_core.messages import AIMessageChunk

from .fakes import TurnModel, tool_call_chunk

RUN_ID = UUID("77777777-7777-7777-7777-777777777777")


class ScriptedGate:
    """按预设决策回应；记录被问过什么。"""

    def __init__(self, decision: Decision = "approved") -> None:
        self.decision = decision
        self.asked: list[dict[str, Any]] = []

    async def request(self, *, approval_id: str, tool_name: str, args: dict[str, Any]) -> Decision:
        self.asked.append({"approval_id": approval_id, "tool_name": tool_name, "args": args})
        return self.decision


def _spec(*, guard: tuple[str, ...] = ("write_todos",)) -> AgentSpec:
    return AgentSpec(
        slug="guarded",
        name="受管控",
        system_prompt="p",
        model=ModelSpec(model="claude-sonnet-5"),
        tool_names=("write_todos",),
        limits=LimitSpec(timeout_s=20, require_approval_for=frozenset(guard)),
    )


def _tool_using_model() -> TurnModel:
    return TurnModel(
        scripts=[
            [
                tool_call_chunk(
                    "write_todos", '{"todos":[{"content":"做事","status":"pending"}]}', "c1"
                )
            ],
            [AIMessageChunk(content="办完了。")],
        ]
    )


async def _collect(spec: AgentSpec, gate: Any) -> list:
    return [
        e
        async for e in run(
            spec,
            run_id=RUN_ID,
            model=_tool_using_model(),
            input_content="记个待办",
            approvals=gate,
        )
    ]


# ---------------------------------------------------------------- 核心时序


async def test_event_arrives_before_gate_blocks() -> None:
    """★ 事件必须先于阻塞送达，否则死锁。

    gate 一直等到消费者确认「已看到 APPROVAL_REQUIRED」才放行。
    如果事件是在 gate 返回之后才送出的，这里会永远等下去 —— 用 5s 超时兜住。
    """
    seen_event = asyncio.Event()

    class BlockingGate:
        async def request(self, *, approval_id: str, tool_name: str, args: dict) -> Decision:
            await asyncio.wait_for(seen_event.wait(), timeout=5.0)
            return "approved"

    events = []
    async for event in run(
        _spec(),
        run_id=RUN_ID,
        model=_tool_using_model(),
        input_content="记个待办",
        approvals=BlockingGate(),
    ):
        events.append(event)
        if event.type is EventType.APPROVAL_REQUIRED:
            seen_event.set()  # ← 只有事件真的流出来了，gate 才会被放行

    types = [e.type for e in events]
    assert EventType.APPROVAL_REQUIRED in types, "审批事件没能在阻塞前送出（死锁）"
    assert EventType.RUN_FINISHED in types


# ---------------------------------------------------------------- 决策路径


async def test_approved_tool_executes() -> None:
    gate = ScriptedGate("approved")
    events = await _collect(_spec(), gate)
    types = [e.type for e in events]

    assert len(gate.asked) == 1
    assert gate.asked[0]["tool_name"] == "write_todos"
    assert EventType.TOOL_COMPLETED in types
    # 批准后工具真的跑了 —— todos 快照出现即为证据
    assert EventType.TODOS_UPDATED in types


async def test_rejection_does_not_kill_the_run() -> None:
    """★ §12.2：拒绝不终止 run，而是把拒绝当作工具结果回给 agent，
    这样它可以换个方案继续，而不是整轮白跑。"""
    events = await _collect(_spec(), ScriptedGate("rejected"))
    types = [e.type for e in events]

    assert types[-1] is EventType.RUN_FINISHED, "拒绝把 run 弄失败了"
    failed = [e for e in events if e.type is EventType.TOOL_FAILED]
    assert failed, "拒绝应当体现为一次失败的工具调用"
    assert REJECTED_RESULT in str(failed[0].data.get("result", ""))
    # 工具没真跑，所以不该有 todos 快照
    assert EventType.TODOS_UPDATED not in types


async def test_expired_is_treated_as_reject() -> None:
    events = await _collect(_spec(), ScriptedGate("expired"))
    types = [e.type for e in events]

    assert types[-1] is EventType.RUN_FINISHED
    failed = [e for e in events if e.type is EventType.TOOL_FAILED]
    assert EXPIRED_RESULT in str(failed[0].data.get("result", ""))


# ---------------------------------------------------------------- 门禁范围


async def test_unguarded_tool_is_not_gated() -> None:
    """没列进 require_approval_for 的工具不该被拦 —— 每个工具都弹窗等于没有门禁。"""
    gate = ScriptedGate("approved")
    events = await _collect(_spec(guard=("some_other_tool",)), gate)

    assert gate.asked == []
    assert EventType.APPROVAL_REQUIRED not in [e.type for e in events]
    assert EventType.TODOS_UPDATED in [e.type for e in events]


async def test_no_gate_injected_means_no_gating() -> None:
    """server 没注入 gate 时不该卡住 —— 宁可不拦也不能挂死。"""
    events = [
        e
        async for e in run(
            _spec(),
            run_id=RUN_ID,
            model=_tool_using_model(),
            input_content="记个待办",
        )
    ]
    types = [e.type for e in events]
    assert EventType.APPROVAL_REQUIRED not in types
    assert types[-1] is EventType.RUN_FINISHED


async def test_seq_gapless_with_approval_events() -> None:
    """契约规则 2：custom 通道插进来的事件也要在同一条 seq 序列上。"""
    events = await _collect(_spec(), ScriptedGate("approved"))
    assert [e.seq for e in events] == list(range(1, len(events) + 1))


@pytest.mark.parametrize("decision", ["approved", "rejected", "expired"])
async def test_approval_event_carries_tool_and_args(decision: Decision) -> None:
    """前端弹窗要展示工具名 + 完整参数（§12.2）。"""
    events = await _collect(_spec(), ScriptedGate(decision))
    event = next(e for e in events if e.type is EventType.APPROVAL_REQUIRED)

    assert event.data["tool_name"] == "write_todos"
    assert event.data["approval_id"]
    assert "todos" in event.data["args"]
    assert event.data["reason"]
