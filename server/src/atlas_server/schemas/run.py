from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

RunStatus = Literal[
    "queued", "running", "succeeded", "failed", "cancelled", "interrupted", "awaiting_approval"
]


class RunCreate(BaseModel):
    """发消息 + 创建 run（文档 §11.2）。"""

    content: list[dict[str, Any]] = Field(
        min_length=1, description="Anthropic content blocks，如 [{'type':'text','text':'...'}]"
    )


class ApprovalDecision(BaseModel):
    """§12.2：批准或拒绝。expired 由服务端判定，不接受客户端提交。"""

    decision: Literal["approved", "rejected"]


class PendingApproval(BaseModel):
    """挂起中的确认。刷新页面后前端靠它把弹窗恢复出来。"""

    id: UUID
    tool_name: str
    args: dict[str, Any]
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
