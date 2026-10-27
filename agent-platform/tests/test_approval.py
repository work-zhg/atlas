"""审批模式：规划 / 手动 / 自动下的判定、请示、挂起与续跑（doc/native-approval-mode-design.html）。

问不问人由「会话的审批模式 × 工具的副作用类别」决定（PermissionMiddleware），不再由作者逐个勾选
必审工具。请示的等待方式是「挂起」（doc/detail/suspension.html §06）：pending 时工具**不执行**、
本段挂起、approval_id 可复现（否则续跑会死循环）。

被测的工具 `send_report` 没有只读注解 → 外部副作用：手动模式请示、自动模式交判断器（P1 还没有
判断器，请示）、规划模式拦下。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from atlas_agent.domain.events import EventType
from atlas_agent.domain.spec import AgentSpec, ApprovalSpec, LimitSpec, ModelSpec
from atlas_engine.contracts import ApprovalState, InvalidSpec, ToolEffect
from atlas_engine.kernel.middleware.approval import (
    EXPIRED_RESULT,
    GATE_FAILED_RESULT,
    REJECTED_RESULT,
    approval_id_for,
)
from atlas_engine.kernel.middleware.permission import (
    EXIT_PLAN_MODE,
    PLAN_BLOCKED_RESULT,
    PLAN_REMINDER,
    PermissionMiddleware,
)
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from tests.graphs import run_agent as run

from .fakes import TurnModel, tool_call_chunk

RUN_ID = UUID("77777777-7777-7777-7777-777777777777")

#: send_report 真正执行过的入参 —— 「工具有没有跑」的唯一证据
EXECUTED: list[str] = []


@pytest.fixture(autouse=True)
def _reset() -> None:
    EXECUTED.clear()


def _send_report(text: str) -> str:
    EXECUTED.append(text)
    return "已发送"


#: 没有只读注解 → 外部副作用
SEND_REPORT = StructuredTool.from_function(
    _send_report, name="send_report", description="把报告发给外部系统。"
)
#: 按 MCP 的约定声明只读 → 任何模式都放行
LOOKUP = StructuredTool.from_function(
    lambda q: "查到了", name="lookup", description="查资料。", metadata={"readOnlyHint": True}
)


class ScriptedGate:
    """按预设状态回应；记录被问过什么。`check` 立刻返回，不阻塞（contracts/approval.py）。"""

    def __init__(self, state: ApprovalState = "approved") -> None:
        self.state = state
        self.asked: list[dict[str, Any]] = []

    async def check(
        self, *, approval_id: str, tool_name: str, args: dict[str, Any], **extra: Any
    ) -> ApprovalState:
        self.asked.append(
            {"approval_id": approval_id, "tool_name": tool_name, "args": args, **extra}
        )
        return self.state


def _spec(mode: str = "manual") -> AgentSpec:
    return AgentSpec(
        slug="modes",
        name="审批模式",
        system_prompt="p",
        model=ModelSpec(model="claude-sonnet-5"),
        limits=LimitSpec(timeout_s=20),
        approval=ApprovalSpec(default_mode=mode),  # type: ignore[arg-type]
    )


def _calls(*calls: tuple[str, str], reply: str = "办完了。") -> TurnModel:
    """模型依次调用这些工具（每轮一个），最后回复。"""
    scripts = [[tool_call_chunk(name, args, f"c{i}")] for i, (name, args) in enumerate(calls, 1)]
    return TurnModel(scripts=[*scripts, [AIMessageChunk(content=reply)]])


def _report_model() -> TurnModel:
    return _calls(("send_report", '{"text":"周报"}'))


async def _collect(
    spec: AgentSpec, gate: Any, *, model: Any = None, mode: str | None = None, **kw: Any
) -> list:
    return [
        e
        async for e in run(
            spec,
            run_id=kw.pop("run_id", RUN_ID),
            model=model or _report_model(),
            input_content=kw.pop("input_content", "发周报"),
            approvals=gate,
            extra_tools=[SEND_REPORT, LOOKUP],
            approval_mode=mode,
            **kw,
        )
    ]


def _failed_results(events: list) -> list[str]:
    return [str(e.data.get("result", "")) for e in events if e.type is EventType.TOOL_FAILED]


# ---------------------------------------------------------------- 判定矩阵（设计 5.2）

_MATRIX = {
    #            read     plan     write    execute  external delegate
    "plan": ("allow", "allow", "block", "block", "block", "block"),
    "manual": ("allow", "allow", "ask", "ask", "ask", "ask"),
    # P1 没有判断器：execute / external 请示；delegate 放行（子 run 继承模式，动作照样受管）
    "auto": ("allow", "allow", "allow", "ask", "ask", "allow"),
}
_EFFECTS = (
    ToolEffect.READ,
    ToolEffect.PLAN,
    ToolEffect.WRITE,
    ToolEffect.EXECUTE,
    ToolEffect.EXTERNAL,
    ToolEffect.DELEGATE,
)


async def _verdict(
    mode: str, tool: str, effects: dict[str, ToolEffect], irreversible: tuple[str, ...] = ()
) -> str:
    """直接调中间件：handler 被调 = 放行；gate 被问 = 请示；都没有 = 拦下。"""
    gate = ScriptedGate("approved")  # 已批：不走 pending，不需要图的 stream writer
    middleware = PermissionMiddleware(
        effects=effects, default_mode="auto", gate=gate, irreversible=irreversible
    )
    request = SimpleNamespace(
        tool_call={"id": "c1", "name": tool, "args": {}},
        tool=object(),
        state={"approval_mode": mode, "messages": []},
        runtime=None,
    )
    ran: list[bool] = []

    async def handler(_req: Any) -> ToolMessage:
        ran.append(True)
        return ToolMessage(content="ok", tool_call_id="c1")

    result = await middleware.awrap_tool_call(request, handler)
    if gate.asked:
        return "ask"
    if ran:
        return "allow"
    assert isinstance(result, ToolMessage) and result.status == "error"
    return "block"


@pytest.mark.parametrize("mode", ["plan", "manual", "auto"])
@pytest.mark.parametrize("index", range(len(_EFFECTS)))
async def test_the_decision_matrix(mode: str, index: int) -> None:
    """★ 模式 × 类别的判定就是设计文档 5.2 那张表。改这张表之前先改设计 —— 它是对用户的承诺。"""
    effect = _EFFECTS[index]
    assert await _verdict(mode, "tool_x", {"tool_x": effect}) == _MATRIX[mode][index]


async def test_an_unknown_tool_is_treated_as_external() -> None:
    """查不到类别的工具按最保守的「外部副作用」判定。"""
    assert await _verdict("manual", "mystery", {}) == "ask"
    assert await _verdict("plan", "mystery", {}) == "block"


async def test_an_irreversible_write_is_not_auto_approved() -> None:
    """删除是不可逆的写入：自动模式下不直接放行（P1 请示）；普通写入照常放行。"""
    effects = {"delete": ToolEffect.WRITE, "write_file": ToolEffect.WRITE}
    assert await _verdict("auto", "delete", effects, irreversible=("delete",)) == "ask"
    assert await _verdict("auto", "write_file", effects, irreversible=("delete",)) == "allow"


# ---------------------------------------------------------------- 请示：挂起与续跑


async def test_pending_suspends_without_executing_the_tool() -> None:
    """★ 核心性质：还没批就**不执行**，本段挂起，且发出审批事件。

    没人知道有审批在等的话，run 会一直挂着。
    """
    events = await _collect(_spec("manual"), ScriptedGate("pending"))
    types = [e.type for e in events]

    assert EventType.APPROVAL_REQUIRED in types
    assert EventType.RUN_SUSPENDED in types, types
    assert EXECUTED == []
    assert EventType.RUN_FINISHED not in types


async def test_the_suspension_records_the_approval_it_waits_on() -> None:
    """挂起时要说清楚在等什么 —— 续跑的 barrier 按 reason 分派。"""
    events = await _collect(_spec("manual"), ScriptedGate("pending"))
    suspended = next(e for e in events if e.type is EventType.RUN_SUSPENDED)
    waits = suspended.data["waiting_on"]
    assert len(waits) == 1
    assert waits[0]["reason"] == "approval"
    assert waits[0]["token"] == approval_id_for(str(RUN_ID), "c1")
    assert waits[0]["tool_call_id"] == "c1"


async def test_pending_writes_no_tool_result() -> None:
    """挂起时不写任何 tool 结果：调用留着悬空，只在 waiting_on 里登记。

    见 contracts/suspension.py。
    """
    model = _report_model()
    events = await _collect(_spec("manual"), ScriptedGate("pending"), model=model)
    types = [e.type for e in events]
    assert model.call_count == 1, "挂起之后不该再有模型调用"
    assert EventType.TOOL_COMPLETED not in types and EventType.TOOL_FAILED not in types


def _resume_history(name: str, args: dict[str, Any]) -> list:
    return [
        HumanMessage(content="发周报"),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": name, "args": args}]),
    ]


async def _resume(
    spec: AgentSpec,
    outcome: dict[str, Any],
    *,
    model: TurnModel,
    mode: str,
    name: str = "send_report",
    args: dict[str, Any] | None = None,
    gate: ScriptedGate | None = None,
) -> list:
    token = approval_id_for(str(RUN_ID), "c1")
    return [
        e
        async for e in run(
            spec,
            run_id=RUN_ID,
            model=model,
            input_content=None,
            history=_resume_history(name, args or {"text": "周报"}),
            resume=True,
            approvals=gate or ScriptedGate("pending"),
            extra_tools=[SEND_REPORT, LOOKUP],
            waiting_on={"c1": {"reason": "approval", "token": token}},
            decisions={token: outcome},
            approval_mode=mode,
        )
    ]


async def test_resume_executes_the_approved_call_without_asking_again() -> None:
    """续跑段：消息原样 + waiting_on + decisions → 直接执行，不再问 gate、不再弹窗。"""
    gate = ScriptedGate("pending")
    model = TurnModel(scripts=[[AIMessageChunk(content="发完了。")]])
    events = await _resume(
        _spec("manual"), {"status": "approved"}, model=model, mode="manual", gate=gate
    )
    types = [e.type for e in events]
    assert gate.asked == [], "决策已经交进来了，不该再问 gate"
    assert EventType.APPROVAL_REQUIRED not in types
    assert EXECUTED == ["周报"]
    assert types[-1] is EventType.RUN_FINISHED


async def test_a_rejection_with_a_reason_reaches_the_model() -> None:
    """拒绝时带的原因作为工具结果交给模型 —— 比只说「不行」更容易改对。"""
    model = TurnModel(scripts=[[AIMessageChunk(content="好，不发了。")]])
    events = await _resume(
        _spec("manual"), {"status": "rejected", "feedback": "周报还没定稿"}, model=model,
        mode="manual",
    )  # fmt: skip
    assert EXECUTED == []
    assert any("周报还没定稿" in r for r in _failed_results(events))


def test_the_approval_id_is_reproducible() -> None:
    """★ 审批能挂起的前提：续跑时算出同一个 id，否则查不到记录 → 当成首次 → 再挂起，死循环。"""
    first = approval_id_for("run-1", "call-1")
    assert first == approval_id_for("run-1", "call-1")
    assert first != approval_id_for("run-2", "call-1")
    assert first != approval_id_for("run-1", "call-2")


async def test_the_gate_is_asked_with_a_reproducible_id_and_the_reason() -> None:
    """问 gate 时用的是可复现的 id，并带上种类、来源与给用户看的原因（落进审批表）。"""
    gate = ScriptedGate("pending")
    await _collect(_spec("manual"), gate)
    assert len(gate.asked) == 1
    asked = gate.asked[0]
    assert asked["approval_id"] == approval_id_for(str(RUN_ID), "c1")
    assert asked["kind"] == "tool"
    assert asked["source"] == "manual_mode"
    assert "手动模式" in asked["reason"]


async def test_the_same_tool_call_id_in_another_run_is_another_approval() -> None:
    """★ approval_id 带上 run_id：两个 run 里同名的 tool_call_id 不能撞成同一条审批。

    修复前 runner 没把 run_id 交进图，id 只由 tool_call_id 派生 —— 后一个 run 查到前一个 run
    的审批行，直接沿用它的状态（测试里假模型固定用 c1，连跑时互相污染）。
    """
    gate = ScriptedGate("pending")
    await _collect(_spec("manual"), gate)
    await _collect(_spec("manual"), gate, run_id=UUID("88888888-8888-8888-8888-888888888888"))
    first, second = (a["approval_id"] for a in gate.asked)
    assert first != second


# ---------------------------------------------------------------- 决策路径


async def test_approved_tool_executes() -> None:
    events = await _collect(_spec("manual"), ScriptedGate("approved"))
    assert EXECUTED == ["周报"]
    assert EventType.TOOL_COMPLETED in [e.type for e in events]


async def test_rejection_does_not_kill_the_run() -> None:
    """★ 拒绝不终止 run，而是作为工具结果回给 agent，它可以换个方案继续。"""
    events = await _collect(_spec("manual"), ScriptedGate("rejected"))
    assert [e.type for e in events][-1] is EventType.RUN_FINISHED
    assert any(REJECTED_RESULT in r for r in _failed_results(events))
    assert EXECUTED == []


async def test_expired_is_treated_as_reject() -> None:
    events = await _collect(_spec("manual"), ScriptedGate("expired"))
    assert [e.type for e in events][-1] is EventType.RUN_FINISHED
    assert any(EXPIRED_RESULT in r for r in _failed_results(events))


async def test_a_broken_gate_does_not_let_the_tool_through() -> None:
    """★ 门禁自己坏了 → 仍然**不放行**：无人把关地执行，比多挡一次严重得多。"""
    events = await _collect(_spec("manual"), ScriptedGate("failed"))
    assert [e.type for e in events][-1] is EventType.RUN_FINISHED
    assert EXECUTED == []


async def test_a_broken_gate_says_it_is_broken_not_that_you_refused() -> None:
    """★ 「系统坏了」和「用户拒绝了」给模型的指令相反：拒绝 → 换方案；故障 → 如实报告，别绕路。"""
    results = _failed_results(await _collect(_spec("manual"), ScriptedGate("failed")))
    assert any(GATE_FAILED_RESULT in r for r in results)
    assert not any(REJECTED_RESULT in r for r in results)


async def test_without_a_gate_a_question_is_not_let_through() -> None:
    """没注入门禁时请示无从登记：按门禁故障处理（不放行、也不挂起），run 照常结束。"""
    events = await _collect(_spec("manual"), None)
    assert EXECUTED == []
    assert EventType.RUN_SUSPENDED not in [e.type for e in events]
    assert any(GATE_FAILED_RESULT in r for r in _failed_results(events))


async def test_a_read_only_tool_is_never_gated() -> None:
    """只读工具（MCP 约定的 readOnlyHint）任何模式都放行 —— 每个工具都弹窗等于没有门禁。"""
    gate = ScriptedGate("approved")
    for mode in ("plan", "manual", "auto"):
        events = await _collect(
            _spec(mode), gate, model=_calls(("lookup", '{"q":"x"}')), mode=mode
        )
        assert EventType.TOOL_COMPLETED in [e.type for e in events], mode
    assert gate.asked == []


async def test_seq_gapless_with_approval_events() -> None:
    """契约规则 2：custom 通道插进来的事件也要在同一条 seq 序列上。"""
    events = await _collect(_spec("manual"), ScriptedGate("pending"))
    assert [e.seq for e in events] == list(range(1, len(events) + 1))


async def test_approval_event_carries_tool_args_and_why() -> None:
    """前端卡片要展示工具名、完整参数和「为什么问」。事件只在 pending 时发。"""
    events = await _collect(_spec("manual"), ScriptedGate("pending"))
    event = next(e for e in events if e.type is EventType.APPROVAL_REQUIRED)
    assert event.data["tool_name"] == "send_report"
    assert event.data["args"] == {"text": "周报"}
    assert event.data["approval_kind"] == "tool"
    assert event.data["source"] == "manual_mode"
    assert event.data["reason"]
    assert event.data["accepts_feedback"] is True, "拒绝原因会交给模型，前端据此显示输入框"


@pytest.mark.parametrize("state", ["approved", "rejected", "expired", "failed"])
async def test_a_settled_approval_does_not_re_prompt(state: ApprovalState) -> None:
    """已经有结论的不再弹窗 —— 那是续跑段每次都会经过的路径。"""
    events = await _collect(_spec("manual"), ScriptedGate(state))
    assert EventType.APPROVAL_REQUIRED not in [e.type for e in events]


# ---------------------------------------------------------------- 三种模式


async def test_plan_mode_blocks_side_effects_without_suspending() -> None:
    """规划模式下有副作用的调用被拦下：不执行、不问人、不挂起，回一条说明让模型先提交计划。"""
    gate = ScriptedGate("approved")
    events = await _collect(_spec("plan"), gate, mode="plan")
    assert EXECUTED == []
    assert gate.asked == []
    assert EventType.RUN_SUSPENDED not in [e.type for e in events]
    assert any(PLAN_BLOCKED_RESULT in r for r in _failed_results(events))


async def test_auto_mode_asks_for_external_tools_until_the_classifier_exists() -> None:
    """自动模式下外部副作用交判断器；P1 还没有判断器，请示，并写明原因。"""
    gate = ScriptedGate("pending")
    events = await _collect(_spec("auto"), gate, mode="auto")
    assert EventType.RUN_SUSPENDED in [e.type for e in events]
    assert gate.asked[0]["source"] == "auto_uncertain"


async def test_plan_mode_appends_a_reminder_to_the_request_only() -> None:
    """★ 规划提醒附在用户本轮那条消息上，只进请求：系统提示词与工具定义不变（prompt 缓存的前缀），
    也不进 transcript（不落库）。"""
    from atlas_agent.domain.messages import Transcript

    model = _calls(("lookup", '{"q":"x"}'))
    transcript = Transcript()
    await _collect(_spec("plan"), ScriptedGate(), model=model, mode="plan", transcript=transcript)
    first_request = model.seen[0]
    user = [m for m in first_request if isinstance(m, HumanMessage)][-1]
    assert PLAN_REMINDER in str(user.content)
    assert not any(PLAN_REMINDER in str(m.content) for m in transcript.messages)


async def test_no_reminder_outside_plan_mode() -> None:
    model = _calls(("lookup", '{"q":"x"}'))
    await _collect(_spec("auto"), ScriptedGate(), model=model, mode="auto")
    assert not any(PLAN_REMINDER in str(m.content) for m in model.seen[0])


# ---------------------------------------------------------------- 计划审批


_PLAN = "目标：发周报。步骤：1) 整理 2) 发送。"


async def test_submitting_a_plan_suspends_for_approval() -> None:
    """exit_plan_mode 登记一次 kind=plan 的审批并挂起；事件带上计划与可选的执行模式。"""
    gate = ScriptedGate("pending")
    model = _calls((EXIT_PLAN_MODE, f'{{"plan":"{_PLAN}"}}'))
    events = await _collect(_spec("plan"), gate, model=model, mode="plan")
    required = next(e for e in events if e.type is EventType.APPROVAL_REQUIRED)
    assert required.data["approval_kind"] == "plan"
    assert required.data["args"] == {"plan": _PLAN}
    assert required.data["execution_modes"] == ["manual", "auto"]
    assert gate.asked[0]["kind"] == "plan"
    assert events[-1].type is EventType.RUN_SUSPENDED


async def test_an_approved_plan_switches_to_the_mode_the_user_picked() -> None:
    """★ 计划获批 → 模式改成用户选的执行模式，同一轮接着按新模式判定。

    续跑后模型调 send_report：规划模式下它会被拦下；切到自动后改为交判断器（P1 请示），
    所以看到的是一次 auto_uncertain 的请示 —— 证明模式真的变了。
    """
    gate = ScriptedGate("pending")
    model = _calls(("send_report", '{"text":"周报"}'))
    events = await _resume(
        _spec("plan"), {"status": "approved", "mode": "auto"}, model=model, mode="plan",
        name=EXIT_PLAN_MODE, args={"plan": _PLAN}, gate=gate,
    )  # fmt: skip
    mode_events = [e for e in events if e.type is EventType.AGENT_MODE]
    assert [e.data["effective"] for e in mode_events] == ["auto"]
    assert mode_events[0].data["source"] == "plan_approved"
    assert gate.asked and gate.asked[0]["source"] == "auto_uncertain"
    assert not any(PLAN_BLOCKED_RESULT in r for r in _failed_results(events))


async def test_a_rejected_plan_stays_in_plan_mode_with_the_feedback() -> None:
    """提了修改意见：意见交给模型，模式不变（还是规划）。"""
    model = TurnModel(scripts=[[AIMessageChunk(content="好，我改。")]])
    events = await _resume(
        _spec("plan"), {"status": "rejected", "feedback": "先补上验收标准"}, model=model,
        mode="plan", name=EXIT_PLAN_MODE, args={"plan": _PLAN},
    )  # fmt: skip
    assert not [e for e in events if e.type is EventType.AGENT_MODE]
    assert any("先补上验收标准" in r for r in _failed_results(events))


async def test_submitting_a_plan_outside_plan_mode_is_refused() -> None:
    """不在规划模式时调 exit_plan_mode：回错误、不登记审批（工具在所有模式下都可见）。"""
    gate = ScriptedGate("pending")
    model = _calls((EXIT_PLAN_MODE, f'{{"plan":"{_PLAN}"}}'))
    events = await _collect(_spec("auto"), gate, model=model, mode="auto")
    assert gate.asked == []
    assert any("不在规划模式" in r for r in _failed_results(events))


def test_a_plan_only_agent_is_refused() -> None:
    """只许规划的智能体，计划没有执行模式可选，永远批不下来 —— 保存时就拦住。"""
    with pytest.raises(InvalidSpec):
        ApprovalSpec(default_mode="plan", allowed_modes=("plan",)).validate()
    ApprovalSpec(default_mode="plan", allowed_modes=("plan", "manual")).validate()
