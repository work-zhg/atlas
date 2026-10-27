"""领域错误。管理 / 内部接口统一翻译成 {"detail": 说明, "code": 错误码, "context": {...}}。

★ 数据面（/mcp/*）的拒绝不走这里：那里要按 MCP 协议回 JSON-RPC 错误体（proxy.py）。
"""

from __future__ import annotations

__all__ = [
    "Conflict",
    "Forbidden",
    "GatewayError",
    "Invalid",
    "NotFound",
    "TooSoon",
    "Unauthorized",
    "Unavailable",
]


class GatewayError(Exception):
    status_code = 400
    default_code = "INVALID"

    def __init__(self, message: str, *, code: str | None = None, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.context = context


class Invalid(GatewayError):
    status_code = 400
    default_code = "INVALID"


class Unauthorized(GatewayError):
    status_code = 401
    default_code = "UNAUTHORIZED"


class Forbidden(GatewayError):
    status_code = 403
    default_code = "FORBIDDEN"


class NotFound(GatewayError):
    status_code = 404
    default_code = "NOT_FOUND"


class Conflict(GatewayError):
    status_code = 409
    default_code = "CONFLICT"


class TooSoon(GatewayError):
    status_code = 429
    default_code = "TOO_SOON"


class Unavailable(GatewayError):
    """依赖的服务（用户中心、上游）暂不可用。判权查不到时一律拒绝，不放行（§5.8）。"""

    status_code = 503
    default_code = "UNAVAILABLE"
