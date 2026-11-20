"""领域错误。API 层统一翻译为 `{code, message, details}`（和用户中心同一种格式）。"""

from __future__ import annotations

from typing import Any

__all__ = ["Conflict", "Forbidden", "Invalid", "NotFound", "OpsError", "Unauthorized", "Upstream"]


class OpsError(Exception):
    status_code = 400

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class Invalid(OpsError):
    status_code = 422


class Unauthorized(OpsError):
    status_code = 401


class Forbidden(OpsError):
    status_code = 403


class NotFound(OpsError):
    status_code = 404


class Conflict(OpsError):
    status_code = 409


class Upstream(OpsError):
    """业务系统调不通（连不上、超时、5xx）。"""

    status_code = 502
