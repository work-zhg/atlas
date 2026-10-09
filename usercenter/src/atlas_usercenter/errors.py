"""领域错误。API 层统一翻译为 `{code, message, details}`（总体设计 §10）。"""

from __future__ import annotations

from typing import Any

__all__ = ["Conflict", "Forbidden", "Invalid", "NotFound", "UCError", "Unauthorized"]


class UCError(Exception):
    status_code = 400

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class Invalid(UCError):
    status_code = 422


class Unauthorized(UCError):
    status_code = 401


class Forbidden(UCError):
    status_code = 403


class NotFound(UCError):
    status_code = 404


class Conflict(UCError):
    status_code = 409
