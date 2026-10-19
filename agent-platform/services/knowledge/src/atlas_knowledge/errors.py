"""领域错误。API 层统一翻译成 HTTP 状态码（api/app.py）。"""

from __future__ import annotations

__all__ = ["Conflict", "Forbidden", "Invalid", "KnowledgeError", "NotFound", "Unavailable"]


class KnowledgeError(Exception):
    status_code = 400

    def __init__(self, message: str, **detail: object) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class Invalid(KnowledgeError):
    status_code = 400


class Forbidden(KnowledgeError):
    status_code = 403


class NotFound(KnowledgeError):
    status_code = 404


class Conflict(KnowledgeError):
    status_code = 409


class Unavailable(KnowledgeError):
    """依赖的服务（用户中心）暂不可用。"""

    status_code = 503
