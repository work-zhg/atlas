"""领域错误。API 层统一翻译成 HTTP 状态码（api/app.py）。"""

from __future__ import annotations

__all__ = [
    "Conflict",
    "Forbidden",
    "Invalid",
    "NotFound",
    "SkillError",
    "Unauthorized",
    "Unavailable",
]


class SkillError(Exception):
    status_code = 400

    def __init__(self, message: str, **detail: object) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class Invalid(SkillError):
    status_code = 400


class Forbidden(SkillError):
    status_code = 403


class NotFound(SkillError):
    status_code = 404


class Conflict(SkillError):
    """状态不允许这个操作（如审查一个已发布的版本）、或与现有数据冲突。"""

    status_code = 409


class Unauthorized(SkillError):
    """没登录、账号停用、不在这个租户里。"""

    status_code = 401


class Unavailable(SkillError):
    """依赖的服务（用户中心）暂时不可用：一律不放行。"""

    status_code = 503
