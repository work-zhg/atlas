"""P7 · 高风险工具的人工确认（文档 §12.2）。

★ 等待方式从「在线阻塞」改成「挂起」（doc/detail/suspension.html §06）。

  原先 gate 一路 await 到有人点头或 600s 超时，于是最危险的失败是**死锁**：
  审批事件必须在 gate 阻塞**之前**送出，否则用户看不到弹窗而后端在等一个
  永远不会来的决策。那条时序测试（test_event_arrives_before_gate_blocks）
  刻意构造成「事件没先到就必然超时」。

  现在 gate 不阻塞了 —— 死锁在结构上不可能发生，那个测试随之消失。取而代之
  的是另一组性质：pending 时工具**不执行**、本段挂起、approval_id 可复现
  （否则续跑会死循环）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from atlas_engine.contracts import ApprovalState
from atlas_engine.kernel.middleware.approval import (
    EXPIRED_RESULT,
    GATE_FAILED_RESULT,
    REJECTED_RESULT,
    approval_id_for,
)
from atlas_server.domain.events import EventType
from atlas_server.domain.spec import AgentSpec, LimitSpec, ModelSpec
from langchain_core.messages import AIMessageChunk

from tests.graphs import run_agent as run

from .fakes import TurnModel, tool_call_chunk

RUN_ID = UUID("77777777-7777-7777-7777-777777777777")


class ScriptedGate:
    """按预设状态回应；记录被问过什么。

    ★ `check` 而不是 `request` —— 它**立刻返回**，不阻塞（contracts/approval.py）。
    """

    def __init__(self, state: ApprovalState = "approved") -> None:
        self.state = state
        self.asked: list[dict[str, Any]] = []

    async def check(
        self, *, approval_id: str, tool_name: str, args: dict[str, Any]
    ) -> ApprovalState:
        self.asked.append({"approval_id": approval_id, "tool_name": tool_name, "args": args})
        return self.state


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


async def _collect(spec: AgentSpec, gate: Any, *, model: Any = None) -> list:
    return [
        e
        async for e in run(
            spec,
            run_id=RUN_ID,
            model=model or _tool_using_model(),
            input_content="记个待办",
            approvals=gate,
        )
    ]


# ---------------------------------------------------------------- 核心时序


async def test_pending_suspends_without_executing_the_tool() -> None:
    """★ 核心性质：还没批就**不执行**，本段挂起。

    执行了再问等于门禁不存在；而阻塞等待（旧行为）会占住一个 asyncio.Task，
    进程重启即丢，且 600s 对小时级任务根本不够。
    """
    gate = ScriptedGate("pending")
    events = await _collect(_spec(), gate)
    types = [e.type for e in events]

    assert EventType.APPROVAL_REQUIRED in types, "弹窗事件没发出去 —— 没人知道有审批在等"
    assert EventType.RUN_SUSPENDED in types, types
    # 工具没跑：todos 快照是它执行过的唯一证据
    assert EventType.TODOS_UPDATED not in types
    # 挂起不是终态 —— 这一轮还会续
    assert EventType.RUN_FINISHED not in types


async def test_the_suspension_records_the_approval_it_waits_on() -> None:
    """挂起时要说清楚在等什么 —— 续跑的 barrier 按 reason 分派。

    只记 token 的话审批挂起会被当成委派：那种 barrier 是「子 run 终态」，而
    审批挂起时没有子 run —— barrier 立刻满足，于是续跑、又挂起，死循环。
    """
    events = await _collect(_spec(), ScriptedGate("pending"))
    suspended = next(e for e in events if e.type is EventType.RUN_SUSPENDED)

    waits = suspended.data["waiting_on"]
    assert len(waits) == 1
    assert waits[0]["reason"] == "approval"
    assert waits[0]["token"], "没记下 approval_id，续跑时无从查状态"


async def test_the_marker_never_reaches_the_model() -> None:
    """哨兵是内部标记 —— 模型看到它会把它当成工具的返回值。"""
    model = _tool_using_model()
    events = await _collect(_spec(), ScriptedGate("pending"), model=model)

    assert model.call_count == 1, "挂起之后不该再有模型调用"
    completed = [e for e in events if e.type is EventType.MESSAGE_COMPLETED]
    text = "".join(
        b.get("text", "") for e in completed for b in e.data.get("content", [])
    )
    assert "__ATLAS_SUSPENDED__" not in text


def test_the_approval_id_is_reproducible() -> None:
    """★ 这是审批能挂起的前提。

    随机 id 在续跑时会算出一个**新的**：gate 查不到记录 → 当成首次 → 返回
    pending → 再挂起一次。死循环，而且看起来像「批准了但没反应」。
    """
    first = approval_id_for("run-1", "call-1")
    assert first == approval_id_for("run-1", "call-1")
    # 不同 run 里出现同一个 tool_call_id 不该撞
    assert first != approval_id_for("run-2", "call-1")
    assert first != approval_id_for("run-1", "call-2")


async def test_the_gate_is_asked_with_a_reproducible_id() -> None:
    """中间件问 gate 时用的就是那个可复现的 id。"""
    gate = ScriptedGate("pending")
    await _collect(_spec(), gate)
    assert len(gate.asked) == 1
    # tool_call_id 是 "c1"（_tool_using_model 的脚本）
    assert gate.asked[0]["approval_id"].count("-") == 4, gate.asked[0]["approval_id"]
    assert gate.asked[0]["approval_id"] == approval_id_for("", "c1")


# ---------------------------------------------------------------- 决策路径


async def test_approved_tool_executes() -> None:
    """已经批过的（续跑段看到的状态）直接执行。"""
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


async def test_a_broken_gate_does_not_let_the_tool_through() -> None:
    """★ 门禁自己坏了 → 仍然**不放行**（§12.2 的红线）。

    这个方向不能搞反：无人把关地执行高风险工具，比多挡一次严重得多。
    """
    events = await _collect(_spec(), ScriptedGate("failed"))
    types = [e.type for e in events]

    assert types[-1] is EventType.RUN_FINISHED, "门禁故障不该炸掉整个 run"
    assert EventType.TODOS_UPDATED not in types, "门禁故障时工具竟然执行了"


async def test_a_broken_gate_says_it_is_broken_not_that_you_refused() -> None:
    """★ 「系统坏了」必须和「用户拒绝了」说成两回事。

    两者都不放行，但给模型的指令相反：拒绝 → 换个方案；故障 → 如实报告，
    别绕路。说错的代价在真机上见过 —— 一次 tool_name 超列宽被当成用户拒绝，
    模型于是认真排查起「子智能体为什么不说话」，而真因是一列 varchar(128)
    （doc/detail/suspension.html §12 修正记录 12）。
    """
    events = await _collect(_spec(), ScriptedGate("failed"))
    failed = [e for e in events if e.type is EventType.TOOL_FAILED]
    assert failed, "门禁故障应当体现为一次失败的工具调用"

    result = str(failed[0].data.get("result", ""))
    assert GATE_FAILED_RESULT in result
    assert REJECTED_RESULT not in result, "把系统故障说成了用户拒绝"


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


async def test_approval_event_carries_tool_and_args() -> None:
    """前端弹窗要展示工具名 + 完整参数（§12.2）。

    ★ 事件**只在 pending 时发**。已经有结论的（approved/rejected/expired）不
      需要弹窗 —— 那是续跑段每次都会经过的路径，重发只会让前端反复开关弹窗。
    """
    events = await _collect(_spec(), ScriptedGate("pending"))
    event = next(e for e in events if e.type is EventType.APPROVAL_REQUIRED)

    assert event.data["tool_name"] == "write_todos"
    assert event.data["approval_id"]
    assert "todos" in event.data["args"]
    assert event.data["reason"]


@pytest.mark.parametrize("state", ["approved", "rejected", "expired", "failed"])
async def test_a_settled_approval_does_not_re_prompt(state: ApprovalState) -> None:
    """续跑段不该再弹一次窗 —— 结论已经有了。"""
    events = await _collect(_spec(), ScriptedGate(state))
    assert EventType.APPROVAL_REQUIRED not in [e.type for e in events]
