"""领域错误。API 层统一翻译成 HTTP 响应：{"detail": 说明, "code": 错误码, "context": {...}}。

★ 带机器可读的 code：客户端按它分支（例如目标主题已被拆分时按 context.catalog 重新选主题），
  不去匹配说明文字。
"""

from __future__ import annotations

__all__ = [
    "Conflict",
    "Forbidden",
    "Invalid",
    "MemoryServiceError",
    "NotFound",
    "TopicGone",
    "Unauthorized",
    "Unavailable",
    "VersionMismatch",
]


class MemoryServiceError(Exception):
    status_code = 400
    default_code = "INVALID"

    def __init__(self, message: str, *, code: str | None = None, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.context = context


class Invalid(MemoryServiceError):
    status_code = 400
    default_code = "INVALID"


class Unauthorized(MemoryServiceError):
    status_code = 401
    default_code = "UNAUTHORIZED"


class Forbidden(MemoryServiceError):
    status_code = 403
    default_code = "FORBIDDEN"


class NotFound(MemoryServiceError):
    status_code = 404
    default_code = "NOT_FOUND"


class Conflict(MemoryServiceError):
    status_code = 409
    default_code = "CONFLICT"


class TopicGone(Conflict):
    """写入的目标主题已被拆分（或删除）：context 里带当前主题目录，客户端据此重新选择（§3.6）。"""

    default_code = "TOPIC_GONE"


class VersionMismatch(Conflict):
    """拆分提交时主题的版本号和客户端分组时看到的不一致：分组期间有新的写入（§3.6）。"""

    default_code = "VERSION_MISMATCH"


class Unavailable(MemoryServiceError):
    """依赖的服务（用户中心）暂不可用。打开数据权限开关的作用域此时一律拒绝（§3.7）。"""

    status_code = 503
    default_code = "UNAVAILABLE"
