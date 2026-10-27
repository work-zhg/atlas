"""向用户提问（doc/ask-user-question-design.html）：ask_user_question 的挂起、续跑与答案。

server 侧的接口链路（答案校验、盲批按推荐）见 test_ask_user_api.py。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from atlas_agent.domain.events import EventType
from atlas_agent.domain.messages import from_storage, to_storage
from atlas_agent.domain.spec import (
    AgentSpec,
    ApprovalSpec,
    LimitSpec,
    ModelSpec,
    SubAgentSpec,
    spec_for_subagent,
)
from atlas_agent.services.compaction import _render
from atlas_engine.contracts import ApprovalState
from atlas_engine.kernel.middleware.approval import REJECTED_RESULT, approval_id_for
from atlas_engine.kernel.middleware.ask_user import (
    ALL_SKIPPED_RESULT,
    ASK_USER_QUESTION,
    NO_GATE_RESULT,
    SUMMARY_INSTRUCTION,
    resolve_answers,
)
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from tests.graphs import run_agent as run

from .fakes import TurnModel, tool_call_chunk

RUN_ID = UUID("55555555-5555-5555-5555-555555555555")
TOKEN = approval_id_for(str(RUN_ID), "c1")

QUESTIONS: list[dict[str, Any]] = [
    {
        "question": "你要做哪类工作流？",
        "header": "业务场景",
        "options": [
            {"label": "审批流", "description": "多级审批，最常见", "recommended": True},
            {"label": "工单流转", "description": "受理、派单、处理、回访"},
            {"label": "数据处理管道", "description": "采集、清洗、入库"},
        ],
    },
    {
        "question": "需要哪些流程能力？",
        "header": "关键能力",
        "multi_select": True,
        "options": [
            {"label": "条件分支", "description": ""},
            {"label": "并行", "description": ""},
            {"label": "超时催办", "description": ""},
        ],
    },
]


class ScriptedGate:
    def __init__(self, state: ApprovalState = "pending") -> None:
        self.state = state
        self.asked: list[dict[str, Any]] = []

    async def check(self, *, approval_id: str, **extra: Any) -> ApprovalState:
        self.asked.append({"approval_id": approval_id, **extra})
        return self.state


def _spec(mode: str = "manual", *, ask: bool = True) -> AgentSpec:
    return AgentSpec(
        slug="clarify",
        name="澄清",
        system_prompt="p",
        model=ModelSpec(model="claude-sonnet-5"),
        tool_names=(ASK_USER_QUESTION,) if ask else (),
        limits=LimitSpec(timeout_s=20),
        approval=ApprovalSpec(default_mode=mode),  # type: ignore[arg-type]
    )


def _asking_model(questions: list[dict[str, Any]] | None = None) -> TurnModel:
    payload = {"questions": QUESTIONS if questions is None else questions}
    args = json.dumps(payload, ensure_ascii=False)
    return TurnModel(
        scripts=[
            [tool_call_chunk(ASK_USER_QUESTION, args, "c1")],
            [AIMessageChunk(content="好的。")],
        ]
    )


async def _collect(spec: AgentSpec, gate: Any, *, model: Any = None, mode: str = "manual") -> list:
    return [
        e
        async for e in run(
            spec,
            run_id=RUN_ID,
            model=model or _asking_model(),
            input_content="做个工作流的原型",
            approvals=gate,
            approval_mode=mode,
        )
    ]


async def _resume(outcome: dict[str, Any], *, mode: str = "manual") -> tuple[list, TurnModel]:
    model = TurnModel(scripts=[[AIMessageChunk(content="需求摘要：……")]])
    history = [
        HumanMessage(content="做个工作流的原型"),
        AIMessage(
            content="",
            tool_calls=[{"id": "c1", "name": ASK_USER_QUESTION, "args": {"questions": QUESTIONS}}],
        ),
    ]
    events = [
        e
        async for e in run(
            _spec(mode),
            run_id=RUN_ID,
            model=model,
            input_content=None,
            history=history,
            resume=True,
            approvals=ScriptedGate("pending"),
            waiting_on={"c1": {"reason": "approval", "token": TOKEN}},
            decisions={TOKEN: outcome},
            approval_mode=mode,
        )
    ]
    return events, model


def _tool_result(model: TurnModel) -> ToolMessage:
    """续跑后模型看到的那条工具结果。"""
    seen = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert len(seen) == 1, seen
    return seen[0]


# ---------------------------------------------------------------- 提问：挂起


async def test_asking_suspends_and_registers_a_question() -> None:
    """★ 首次调用：登记 kind=question、发卡片事件、挂起，不写工具结果。"""
    gate = ScriptedGate("pending")
    events = await _collect(_spec(), gate)
    types = [e.type for e in events]

    asked = gate.asked[0]
    assert asked["approval_id"] == TOKEN
    assert asked["kind"] == "question" and asked["source"] == "ask_user"
    assert [q["header"] for q in asked["args"]["questions"]] == ["业务场景", "关键能力"]

    required = next(e for e in events if e.type is EventType.APPROVAL_REQUIRED)
    assert required.data["approval_kind"] == "question"
    assert required.data["tool_call_id"] == "c1", "前端靠它把对应的工具行收起来"
    assert required.data["args"]["questions"][0]["options"][0]["recommended"] is True

    suspended = next(e for e in events if e.type is EventType.RUN_SUSPENDED)
    assert suspended.data["waiting_on"] == [
        {"tool_call_id": "c1", "reason": "approval", "token": TOKEN}
    ]
    assert EventType.TOOL_COMPLETED not in types and EventType.TOOL_FAILED not in types


async def test_asking_is_allowed_in_plan_mode() -> None:
    """规划模式下也能问（问用户不改变任何东西）—— 不能被当成有副作用的调用拦下。"""
    gate = ScriptedGate("pending")
    events = await _collect(_spec("plan"), gate, mode="plan")
    assert gate.asked and gate.asked[0]["kind"] == "question"
    assert EventType.TOOL_FAILED not in [e.type for e in events], "被当成有副作用的调用拦下了"
    assert events[-1].type is EventType.RUN_SUSPENDED


async def test_invalid_questions_go_back_to_the_model_without_asking() -> None:
    """参数不合法（选项只有一个、两个推荐）：回工具错误让模型改，不登记、不挂起。"""
    bad = [
        {
            "question": "选哪个？",
            "header": "x",
            "options": [
                {"label": "A", "description": "", "recommended": True},
                {"label": "B", "description": "", "recommended": True},
            ],
        }
    ]
    gate = ScriptedGate("pending")
    events = await _collect(_spec(), gate, model=_asking_model(bad))
    assert gate.asked == []
    failed = [e for e in events if e.type is EventType.TOOL_FAILED]
    assert [e.data["call_id"] for e in failed] == ["c1"]
    assert events[-1].type is EventType.RUN_FINISHED


async def test_without_a_gate_the_question_is_not_a_dead_end() -> None:
    """没有门禁（没人能回答）：不挂起，告诉模型按推荐继续 —— 一轮不能卡死在这里。"""
    events = await _collect(_spec(), None)
    failed = [e for e in events if e.type is EventType.TOOL_FAILED]
    assert failed and failed[0].data.get("result") == NO_GATE_RESULT
    assert events[-1].type is EventType.RUN_FINISHED


async def test_the_tool_exists_only_when_enabled() -> None:
    """没勾这个工具的智能体看不到它：模型调了也只是「不是有效工具」，不会登记问题。"""
    gate = ScriptedGate("pending")
    await _collect(_spec(ask=False), gate)
    assert gate.asked == []


# ---------------------------------------------------------------- 回答：续跑


async def test_answers_reach_the_model_and_the_card() -> None:
    """★ 续跑：答案整理成工具结果交给模型，并发 question.answered（卡片显示答案）。"""
    answers = [
        {"selected": ["工单流转"], "other": None, "by": "user"},
        {"selected": ["条件分支", "并行"], "other": "还要能回退", "by": "user"},
    ]
    events, model = await _resume({"status": "approved", "answers": answers})
    assert _tool_result(model).content == "\n".join(
        [
            "用户的回答：",
            "1. 业务场景（你要做哪类工作流？）→ 工单流转",
            "2. 关键能力（需要哪些流程能力？）→ 条件分支、并行、其他：「还要能回退」",
            SUMMARY_INSTRUCTION,
        ]
    )

    answered = next(e for e in events if e.type is EventType.QUESTION_ANSWERED)
    assert answered.data["approval_id"] == TOKEN and answered.data["tool_call_id"] == "c1"
    assert [a["header"] for a in answered.data["answers"]] == ["业务场景", "关键能力"]
    assert answered.data["answers"][0]["selected"] == ["工单流转"]
    assert events[-1].type is EventType.RUN_FINISHED


async def test_skipping_everything_leaves_the_choice_to_the_agent() -> None:
    """「跳过，你来定」：每题都是 skipped → 按推荐继续并写明假设。"""
    skipped = [{"selected": [], "other": None, "by": "skipped"}] * 2
    _, model = await _resume({"status": "approved", "answers": skipped})
    assert _tool_result(model).content == ALL_SKIPPED_RESULT


async def test_approving_without_answers_takes_the_recommendations() -> None:
    """不带答案的批准（不认识问题的调用方）= 全部按推荐；没有推荐项的题交给智能体定。"""
    _, model = await _resume({"status": "approved"})
    assert _tool_result(model).content == "\n".join(
        [
            "用户的回答：",
            "1. 业务场景（你要做哪类工作流？）→ 审批流（用户没有逐题作答，按你的推荐）",
            "2. 关键能力（需要哪些流程能力？）→ 用户没有回答，交给你决定："
            "用你推荐的选项，并在回复里写明这个假设",
            SUMMARY_INSTRUCTION,
        ]
    )


async def test_declining_in_manual_mode_is_not_a_refused_tool() -> None:
    """★ 用户不按选项回答（rejected + 原话）：原话交给模型。

    手动模式下 PermissionMiddleware 也在图里 —— 问题的等待项不能落到它手里，否则这里会变成
    「用户拒绝执行该工具」（设计里提问中间件装在它外层的原因）。
    """
    events, model = await _resume(
        {"status": "rejected", "feedback": "我要的是 BPMN 编辑器"}, mode="manual"
    )
    result = _tool_result(model)
    assert result.content == (
        "用户没有按选项回答，原话：「我要的是 BPMN 编辑器」。按用户的原话理解需求。"
    )
    assert result.content != REJECTED_RESULT and result.status != "error"
    answered = next(e for e in events if e.type is EventType.QUESTION_ANSWERED)
    assert answered.data["answers"] == [] and answered.data["feedback"] == "我要的是 BPMN 编辑器"


# ---------------------------------------------------------------- 答案校验（server 用它判 400）


def test_answers_are_checked_against_the_questions() -> None:
    ok = resolve_answers(QUESTIONS, [{"selected": ["审批流"]}, {"other": "回退"}])
    assert [a["by"] for a in ok] == ["user", "user"]
    assert ok[1] == {"selected": [], "other": "回退", "by": "user"}




@pytest.mark.parametrize(
    "answers",
    [
        pytest.param([{"selected": ["审批流"]}], id="条数和问题对不上"),
        pytest.param([{"selected": ["BPMN"]}, {}], id="选项不存在"),
        pytest.param([{"selected": ["审批流", "工单流转"]}, {}], id="单选题选了两个"),
        pytest.param([{"selected": ["审批流"], "other": "也要工单"}, {}], id="单选题又选又填"),
        pytest.param([{}, {"selected": ["并行", "并行"]}], id="选项重复"),
        pytest.param([{}, {"other": "长" * 501}], id="其他太长"),
    ],
)
def test_answers_that_do_not_fit_the_questions_are_refused(answers: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError):
        resolve_answers(QUESTIONS, answers)


def test_no_answers_means_the_recommendations() -> None:
    resolved = resolve_answers(QUESTIONS, None)
    assert resolved[0] == {"selected": ["审批流"], "other": None, "by": "recommended"}
    assert resolved[1] == {"selected": [], "other": None, "by": "skipped"}, "没有推荐项就交给智能体"


def test_an_empty_answer_is_a_skip() -> None:
    resolved = resolve_answers(QUESTIONS, [{}, {"selected": [], "other": "  "}])
    assert [a["by"] for a in resolved] == ["skipped", "skipped"]


# ---------------------------------------------------------------- SubAgent


def test_a_subagent_does_not_ask_the_user() -> None:
    """子智能体不向用户提问（设计 D6）：派生子 spec 时去掉这个工具，其余工具照常。"""
    parent = AgentSpec(
        slug="lead",
        name="主",
        system_prompt="p",
        model=ModelSpec(model="claude-sonnet-5"),
        tool_names=(ASK_USER_QUESTION, "task"),
        subagents=(
            SubAgentSpec(
                name="writer",
                description="写",
                system_prompt="p",
                model=ModelSpec(model="claude-sonnet-5"),
                tool_names=(ASK_USER_QUESTION, "write_todos"),
            ),
        ),
    )
    child = spec_for_subagent(parent, "writer")
    assert ASK_USER_QUESTION not in child.tool_names
    assert "write_todos" in child.tool_names


# ---------------------------------------------------------------- 压缩


def test_answers_survive_compaction_as_user_messages() -> None:
    """★ 用户的回答是工具结果，但摘要里要算用户说的话 —— 否则第 6 段「全部用户消息（不含工具结果）」
    恰好把它排除，长对话压缩后用户确认过的需求就丢了。

    按工具名判断，所以先过一遍落库 / 读回：工具名要经得起存储往返（domain/messages.py）。
    """
    stored = to_storage(
        [
            HumanMessage(content="做个工作流的原型"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "c1", "name": ASK_USER_QUESTION, "args": {"questions": QUESTIONS}},
                    {"id": "c2", "name": "read_file", "args": {"file_path": "/a.md"}},
                ],
            ),
            ToolMessage(content="用户的回答：审批流", tool_call_id="c1", name=ASK_USER_QUESTION),
            ToolMessage(content="# a", tool_call_id="c2", name="read_file"),
        ]
    )
    rendered = _render(from_storage(stored))
    assert rendered.splitlines()[-2:] == ["[用户（回答提问）] 用户的回答：审批流", "[工具返回] # a"]
