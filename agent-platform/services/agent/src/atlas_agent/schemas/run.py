from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from atlas_agent.domain.events import TraceEvent

#: 审批模式（doc/native-approval-mode-design.html）
ApprovalModeIn = Literal["plan", "manual", "auto"]

#: ★ 'suspended' 是**非终态**：这一轮还没结束，只是在等子智能体的结果
#:   （可能等一小时）。前端应当显示「等待中」而不是把这一轮标成完成 ——
#:   真正的结论要等续跑那一段（domain/events.py::RUN_SUSPENDED）。
RunStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
    "awaiting_approval",
    "suspended",
]


class RunCreate(BaseModel):
    """发消息 + 创建 run（文档 §11.2）。"""

    content: list[dict[str, Any]] = Field(
        min_length=1, description="Anthropic content blocks，如 [{'type':'text','text':'...'}]"
    )
    #: 用户在输入框旁选的审批模式。必须在智能体允许的范围里；会话会记住它，作用于之后的消息。
    #: 不传 = 沿用会话上次的选择（没有就用智能体默认）
    approval_mode: ApprovalModeIn | None = None


class QuestionAnswerIn(BaseModel):
    """ask_user_question 一题的答案。两项都空 = 这题跳过，交给智能体定。"""

    #: 选中的选项 label（单选题最多一个）
    selected: list[str] = Field(default_factory=list, max_length=4)
    #: 「其他」里填的话（单选题填了它就不能再选选项）
    other: str | None = Field(default=None, max_length=500)


class ApprovalDecision(BaseModel):
    """§12.2：批准或拒绝。expired 由服务端判定，不接受客户端提交。"""

    decision: Literal["approved", "rejected"]
    #: 计划审批（kind=plan）批准时必填：用户选的执行模式（手动 / 自动，须在智能体允许的范围里）
    approval_mode: Literal["manual", "auto"] | None = None
    #: 拒绝原因 / 计划的修改意见，作为工具结果交给模型 —— 比只说「不行」更容易改对
    feedback: str | None = Field(default=None, max_length=4000)
    #: 问题（kind=question）的答案，与问题一一对应。批准时不带 = 全部按推荐（不认识问题的调用方）
    answers: list[QuestionAnswerIn] | None = Field(default=None, max_length=4)


class PendingApproval(BaseModel):
    """挂起中的确认。刷新页面后前端靠它把弹窗恢复出来。"""

    id: UUID
    tool_name: str
    args: dict[str, Any]
    #: tool = 工具调用的确认；plan = 规划模式提交的计划；question = 向用户提的问题（args.questions）
    kind: str = "tool"
    reason: str | None = None
    created_at: datetime


class PendingApprovalList(BaseModel):
    data: list[PendingApproval]


class ApprovalOut(BaseModel):
    approval_id: UUID
    run_id: UUID
    decision: str


class RunAccepted(BaseModel):
    run_id: UUID
    #: ★ 幂等重复提交（同一个 Idempotency-Key 再来一次）返回已有的 run，
    #:   但**不会**再写一条消息 —— 此时该字段是全零 UUID。
    #:   前端据此判断"这次没产生新消息"，不要拿它去定位消息行。
    message_id: UUID
    status: RunStatus


class RunOut(BaseModel):
    id: UUID
    thread_id: UUID
    #: 非空即「这是一次委派产生的子 run」。前端据此把它折进父 run 的
    #: task 工具调用下面，而不是当成一条独立的对话轮次。
    parent_run_id: UUID | None = None
    status: RunStatus
    error_kind: str | None
    error_message: str | None
    last_seq: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    thinking_tokens: int
    total_tokens: int
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class RunTraceOut(BaseModel):
    """一轮的归档轨迹（含子 run），按 thread_seq 正序。

    ★ 不含 message.delta（归档时就滤掉了），正文由 message.completed 一次给全。
    ★ 空列表 = 归档已被清理，前端退回只显示消息正文。
    """

    data: list[TraceEvent]
