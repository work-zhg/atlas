"""server 侧领域错误 → HTTP 状态码。

统一错误体（文档 §11）：{"error": {"kind", "message", "details"}}
engine 的 EngineError 由 main.py 另有一条处理链路。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code: int = 400
    kind: str = "bad_request"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(AppError):
    status_code = 404
    kind = "not_found"


class Conflict(AppError):
    status_code = 409
    kind = "conflict"


class SlugTaken(Conflict):
    kind = "slug_taken"


class BuiltinAgentProtected(Conflict):
    kind = "builtin_agent_protected"


class ThreadLocked(Conflict):
    kind = "thread_locked"


class BadCursor(AppError):
    status_code = 400
    kind = "invalid_cursor"
