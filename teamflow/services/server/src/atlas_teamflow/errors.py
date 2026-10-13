"""领域错误。API 层统一翻译为 `{code, message, details}`。"""

from __future__ import annotations

from typing import Any

__all__ = ["Conflict", "Forbidden", "Invalid", "NotFound", "TFError", "Unauthorized", "Upstream"]


class TFError(Exception):
    status_code = 400

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class Invalid(TFError):
    status_code = 422


class Unauthorized(TFError):
    status_code = 401


class Forbidden(TFError):
    status_code = 403


class NotFound(TFError):
    status_code = 404


class Conflict(TFError):
    status_code = 409


class Upstream(TFError):
    """用户中心 / Atlas 不可用或返回意外结果。"""

    status_code = 502
