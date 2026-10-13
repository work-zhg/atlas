"""会话与轮次的状态、一轮的结果（Bridge 设计 §4.6 · §5.5）。"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from ._model import JsonObject, Model

__all__ = [
    "CancelCause",
    "Cancelled",
    "Completed",
    "ErrorInfo",
    "FailCause",
    "Failed",
    "Outcome",
    "SessionState",
    "TurnState",
]


class TurnState(StrEnum):
    RUNNING = "running"
    AWAITING_PERMISSION = "awaiting_permission"
    CANCELLING = "cancelling"
    ENDED = "ended"


class SessionState(StrEnum):
    """server 可见的会话状态（attach 的响应里给出）。bridge 内部的状态更细，见 §5.5。"""

    READY = "ready"
    IN_TURN = "in_turn"
    RECOVERING = "recovering"
    ENDED = "ended"


class CancelCause(StrEnum):
    REQUESTED = "requested"  # server 发来 turn.cancel
    DEADLINE = "deadline"  # 截止到点
    IDLE = "idle"  # 静默到点
    UPSTREAM_LOST = "upstream_lost"  # 断线且超过重连窗口
    SESSION_CLOSING = "session_closing"  # 收到 session.close 时这一轮还在进行
    POD_TERMINATING = "pod_terminating"  # 收到 SIGTERM


class FailCause(StrEnum):
    AGENT_ERROR = "agent_error"  # agent 对 session/prompt 回了错误
    AGENT_EXITED = "agent_exited"  # agent 进程退出
    CANCEL_UNANSWERED = "cancel_unanswered"  # 取消后宽限内无响应，只能终止 agent
    #: 要求的权限模式比能生效的更保守，却设置不上：宁可不跑，也不以更宽松的权限跑
    MODE_UNAVAILABLE = "mode_unavailable"


class ErrorInfo(Model):
    """结构化的错误原因。面向用户的文案由 server 生成（§3.5）。"""

    cause: str
    detail: str | None = None
    #: agent 返回的 ACP 错误（code / message / data），原样
    acp: JsonObject | None = None


class Completed(Model):
    kind: Literal["completed"] = "completed"
    #: ACP 的结束原因原样带上：end_turn · max_tokens · max_turn_requests · refusal
    stop_reason: str
    #: agent 对 session/prompt 的响应原样（usage、_meta 等由 server 的翻译层读取，§3 D2）
    response: JsonObject | None = None


class Cancelled(Model):
    """这一轮被取消，且 agent 已确认。"""

    kind: Literal["cancelled"] = "cancelled"
    cause: CancelCause
    #: agent 以 cancelled 结束时的 prompt 响应原样（可能带已花掉的 usage）
    response: JsonObject | None = None


class Failed(Model):
    kind: Literal["failed"] = "failed"
    cause: FailCause
    error: ErrorInfo | None = None


Outcome = Annotated[Completed | Cancelled | Failed, Field(discriminator="kind")]
