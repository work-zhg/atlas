"""向用户提问：需求不清楚时用选择题问清楚，回答作为工具结果返回（doc/ask-user-question-design.html）。

问题是一种「等人决定的事项」，走审批同一条路（approval.kind = question）：首次调用时登记、在
state.waiting_on 里登记一项、不写结果，本段随即挂起；用户回答后续跑，答案从 state.decisions 取出，
整理成工具结果交给模型 —— 同一轮继续，不用等用户另发一条消息。

★ 「何时问、怎么问」的通用标准写在工具说明里（设计 6.1）；这个领域该问哪几项由智能体作者写在
  系统提示词里（6.2）。
★ 推荐项是选项的属性 recommended，不是选项文字里的标记。
★ 装在 PermissionMiddleware 外层，自己处理自己的工具：问用户在任何审批模式下都允许，而且续跑时
  这个调用的等待项（reason=approval）不能落到 PermissionMiddleware 手里 —— 它会把「用户不想回答」
  当成「用户拒绝执行该工具」。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field, ValidationError, model_validator

from atlas_engine.contracts import ApprovalGate, QuestionAnswer
from atlas_engine.kernel.middleware.approval import approval_id_for, run_id_of
from atlas_engine.kernel.middleware.suspension import SuspensionState, release_wait

__all__ = [
    "ASK_USER_QUESTION",
    "AskUserMiddleware",
    "AskUserQuestionInput",
    "answers_text",
    "resolve_answers",
]

#: 沿用 Claude Code AskUserQuestion 的约定，取蛇形名（同 exit_plan_mode 的取名规则）
ASK_USER_QUESTION = "ask_user_question"

#: 「其他」里能填多长
OTHER_MAX_CHARS = 500

ASK_USER_DESCRIPTION = """向用户提出 1–4 个选择题，等用户回答后继续。用户回答之前这一轮会暂停（可能等很久），回答会作为这个工具的结果返回。

什么时候用：
- 需求有多种理解，按不同理解做出来的结果差别很大。先在心里列出 2–3 种可能的理解，差别大就问。
  例：「做个工作流原型」——审批流、工单、数据管道是完全不同的东西
- 缺的信息猜错了要大量返工，而且没有通行的默认做法
- 在开工前、委派前、提交计划前，把关键决策一次问清

什么时候不用：
- 能从对话、用户记忆、知识库、工作区文件里查到的——先查
- 有通行默认做法的、纯风格偏好、实现细节——用默认值，在回复里写明假设
- 用户说过「直接做」「你定」「别问了」
- 只是想确认「可以开始了吗」——直接开始；需要用户批准方案时，规划模式下用 exit_plan_mode
- 同一个需求已经问过两轮——用推荐的做法继续，写明假设

怎么问：
- 一次问完，最多 4 题；只问答案会改变你下一步做法的问题，按「猜错的返工量 × 你有多不确定」挑最重要的
- 每题 2–4 个互斥选项，写清楚区别；需要多选时设 multi_select。界面会自动加「其他」，不要自己写「其他」选项
- 你倾向的选项设 recommended: true，并在它的 description 里用一句话说明理由
- header 是 12 字以内的短标签，如「业务场景」「保真度」
- 单独调用这个工具，不要和其它工具放在同一批

拿到回答后：先用一段「需求摘要」复述确认后的需求；之后的计划、任务书、产出都以它为准。
用户跳过的题，用你推荐的选项，并写明这是假设。"""

SUMMARY_INSTRUCTION = "请先用一段「需求摘要」复述确认后的需求；之后的计划、任务书和产出都以它为准。"
ALL_SKIPPED_RESULT = "用户选择不回答，让你决定：按你推荐的选项继续，并在回复开头写明这些假设。"
NO_GATE_RESULT = "当前不能向用户提问：按你推荐的选项继续，并在回复里写明假设。"
GATE_FAILED_RESULT = (
    "提问没有送达用户（审批系统故障，不是用户不回答）：按你推荐的选项继续，并在回复里写明假设。"
)
EXPIRED_RESULT = "这次提问已失效：按你推荐的选项继续，并在回复里写明假设。"


class QuestionOption(BaseModel):
    label: str = Field(min_length=1, max_length=40, description="选项名，简短（如「审批流」）")
    description: str = Field(
        default="", max_length=200, description="这个选项意味着什么、和其它选项的区别"
    )
    recommended: bool = Field(
        default=False,
        description="你倾向的选项设为 true（每题最多一个），并在 description 里说明理由",
    )


class Question(BaseModel):
    question: str = Field(
        min_length=1, max_length=300, description="完整的问句，以问号结尾（如「你要做哪类工作流？」）"
    )
    header: str = Field(
        min_length=1, max_length=12, description="12 字以内的短标签，卡片上显示（如「业务场景」）"
    )
    options: list[QuestionOption] = Field(
        min_length=2, max_length=4, description="2–4 个选项；界面会自动加「其他」，不要自己写"
    )
    multi_select: bool = Field(default=False, description="允许多选时设为 true")

    @model_validator(mode="after")
    def _check_options(self) -> Question:
        labels = [o.label for o in self.options]
        if len(set(labels)) != len(labels):
            raise ValueError("同一题的选项 label 不能重复")
        if sum(o.recommended for o in self.options) > 1:
            raise ValueError("每题最多一个 recommended 选项")
        return self


class AskUserQuestionInput(BaseModel):
    questions: list[Question] = Field(
        min_length=1, max_length=4, description="1–4 个问题，一次问完"
    )


def resolve_answers(
    questions: Sequence[Mapping[str, Any]], answers: Sequence[Mapping[str, Any]] | None
) -> list[QuestionAnswer]:
    """用户提交的答案 → 每题一条带来源的答案。不合法时抛 ValueError（server 转成 400）。

    answers 为 None：调用方没给答案就批准了（不认识问题的调用方，如 bench）= 全部按推荐（设计 D8）；
    没有推荐项的题记为跳过。某题既没选也没填 = 这题跳过，交给智能体定。
    """
    parsed = AskUserQuestionInput.model_validate({"questions": list(questions)}).questions
    if answers is None:
        return [_recommended(q) for q in parsed]
    if len(answers) != len(parsed):
        raise ValueError(f"答案数（{len(answers)}）与问题数（{len(parsed)}）不一致")
    out: list[QuestionAnswer] = []
    for index, (question, answer) in enumerate(zip(parsed, answers, strict=True), 1):
        selected = [str(s) for s in answer.get("selected") or []]
        other = str(answer.get("other") or "").strip() or None
        labels = {o.label for o in question.options}
        unknown = [s for s in selected if s not in labels]
        if unknown:
            raise ValueError(f"第 {index} 题没有这些选项：{unknown}")
        if len(set(selected)) != len(selected):
            raise ValueError(f"第 {index} 题的选项重复了")
        if not question.multi_select and len(selected) + (other is not None) > 1:
            raise ValueError(f"第 {index} 题是单选题：只能选一个选项，或者只填「其他」")
        if other is not None and len(other) > OTHER_MAX_CHARS:
            raise ValueError(f"第 {index} 题「其他」不能超过 {OTHER_MAX_CHARS} 字")
        by = "skipped" if not selected and other is None else "user"
        out.append({"selected": selected, "other": other, "by": by})
    return out


def answers_text(questions: Sequence[Question], answers: Sequence[QuestionAnswer]) -> str:
    """答案 → 交给模型的工具结果。"""
    if all(a["by"] == "skipped" for a in answers):
        return ALL_SKIPPED_RESULT
    lines = ["用户的回答："]
    for index, (question, answer) in enumerate(zip(questions, answers, strict=False), 1):
        head = f"{index}. {question.header}（{question.question}）→ "
        if answer["by"] == "skipped":
            lines.append(f"{head}用户没有回答，交给你决定：用你推荐的选项，并在回复里写明这个假设")
            continue
        parts = list(answer["selected"])
        if answer["other"]:
            parts.append(f"其他：「{answer['other']}」")
        suffix = "（用户没有逐题作答，按你的推荐）" if answer["by"] == "recommended" else ""
        lines.append(f"{head}{'、'.join(parts)}{suffix}")
    lines.append(SUMMARY_INSTRUCTION)
    return "\n".join(lines)


def _recommended(question: Question) -> QuestionAnswer:
    picked = [o.label for o in question.options if o.recommended]
    return {"selected": picked, "other": None, "by": "recommended" if picked else "skipped"}


def _ask_user_question_tool() -> BaseTool:
    """只提供 schema 与描述，函数体永远不会被执行 —— 行为全在 awrap_tool_call 里（同 exit_plan_mode）。"""

    def ask_user_question(questions: list[dict[str, Any]]) -> str:
        msg = "ask_user_question 不会被直接执行：AskUserMiddleware 在 awrap_tool_call 里接管"
        raise RuntimeError(msg)

    async def aask_user_question(questions: list[dict[str, Any]]) -> str:
        return ask_user_question(questions)

    return StructuredTool.from_function(
        name=ASK_USER_QUESTION,
        func=ask_user_question,
        coroutine=aask_user_question,
        description=ASK_USER_DESCRIPTION,
        infer_schema=False,
        args_schema=AskUserQuestionInput,
    )


class AskUserMiddleware(AgentMiddleware):
    """提供 ask_user_question：登记问题、挂起，续跑时把用户的回答交给模型。"""

    state_schema = SuspensionState

    def __init__(self, *, gate: ApprovalGate | None) -> None:
        """gate 为 None（单测、没有门禁）时不挂起：回一条「按推荐继续」—— 没有人能回答的地方不能卡住一轮。"""
        super().__init__()
        self._gate = gate
        self.tools = [_ask_user_question_tool()]

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call or {}
        if call.get("name") != ASK_USER_QUESTION:
            return await handler(request)
        call_id = str(call.get("id") or "")
        state = getattr(request, "state", None) or {}

        # ① 续跑：这个调用登记过等待项，答案在 decisions 里
        wait = (state.get("waiting_on") or {}).get(call_id)
        if wait is not None:
            token = str(wait.get("token") or "")
            outcome = (state.get("decisions") or {}).get(token)
            if outcome is None:
                # barrier 只在有结果时续跑，走不到这里；真走到了就保持登记，不重复登记
                return Command(update={"waiting_on": {call_id: wait}})
            return release_wait(self._settle(call, token, outcome), call_id)

        # ② 首次调用：先校验（模型给的参数不合法就让它改，不登记）
        try:
            parsed = AskUserQuestionInput.model_validate(call.get("args") or {})
        except ValidationError as exc:
            return _failed(call_id, f"参数不合法，请按说明修正后重新调用：{_brief(exc)}")
        if self._gate is None:
            return _failed(call_id, NO_GATE_RESULT)
        args = parsed.model_dump()
        approval_id = approval_id_for(run_id_of(request), call_id)
        status = await self._gate.check(
            approval_id=approval_id,
            tool_name=ASK_USER_QUESTION,
            args=args,
            kind="question",
            source="ask_user",
        )
        if status != "pending":
            # 已经有结果（接管后重放到这里）或门禁故障：按结果处理，不再挂起
            return self._settle({**call, "args": args}, approval_id, {"status": status})

        from langgraph.config import get_stream_writer

        # ★ 事件必须发 —— 前端的问题卡片由它驱动。tool_call_id 让前端把对应的工具行收起来
        get_stream_writer()(
            {
                "kind": "approval.required",
                "approval_id": approval_id,
                "tool_name": ASK_USER_QUESTION,
                "tool_call_id": call_id,
                "args": args,
                "reason": None,
                "approval_kind": "question",
                "source": "ask_user",
                "accepts_feedback": True,
            }
        )
        return Command(
            update={"waiting_on": {call_id: {"reason": "approval", "token": approval_id}}}
        )

    def _settle(
        self, call: Mapping[str, Any], approval_id: str, outcome: Mapping[str, Any] | str
    ) -> ToolMessage:
        """按用户的回答生成工具结果，并发 question.answered（卡片据此显示答案，刷新后也在）。"""
        from langgraph.config import get_stream_writer

        call_id = str(call.get("id") or "")
        if isinstance(outcome, str):  # 兜底：旧的字符串形态
            outcome = {"status": outcome}
        status = outcome.get("status")
        try:
            questions = AskUserQuestionInput.model_validate(call.get("args") or {}).questions
        except ValidationError:  # 登记前校验过，走不到这里；真走到了也要给出结果，不能让一轮崩掉
            questions = []

        if status == "approved":
            answers = outcome.get("answers") or resolve_answers(
                [q.model_dump() for q in questions], None
            )
            event_answers = [
                {"header": q.header, "question": q.question, **a}
                for q, a in zip(questions, answers, strict=False)
            ]
            get_stream_writer()(
                {
                    "kind": "question.answered",
                    "approval_id": approval_id,
                    "tool_call_id": call_id,
                    "answers": event_answers,
                }
            )
            return ToolMessage(
                content=answers_text(questions, answers),
                tool_call_id=call_id,
                name=ASK_USER_QUESTION,
            )
        if status == "rejected":
            # 用户没按选项回答（接口以 rejected 提交）：有原话就把原话交给模型
            feedback = str(outcome.get("feedback") or "").strip()
            get_stream_writer()(
                {
                    "kind": "question.answered",
                    "approval_id": approval_id,
                    "tool_call_id": call_id,
                    "answers": [],
                    "feedback": feedback or None,
                }
            )
            text = (
                f"用户没有按选项回答，原话：「{feedback}」。按用户的原话理解需求。"
                if feedback
                else ALL_SKIPPED_RESULT
            )
            return ToolMessage(content=text, tool_call_id=call_id, name=ASK_USER_QUESTION)
        if status == "expired":
            return _failed(call_id, EXPIRED_RESULT)
        # failed 或认不出的值：门禁故障。★ 不能当成 pending 再登记一次 —— 来回跳到 GraphRecursionError
        return _failed(call_id, GATE_FAILED_RESULT)


def _brief(exc: ValidationError) -> str:
    """校验错误 → 给模型看的一行（最多三条）。"""
    parts = []
    for err in exc.errors()[:3]:
        where = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{where}: {err.get('msg')}" if where else str(err.get("msg")))
    return "；".join(parts)


def _failed(call_id: str, text: str) -> ToolMessage:
    return ToolMessage(content=text, tool_call_id=call_id, name=ASK_USER_QUESTION, status="error")
