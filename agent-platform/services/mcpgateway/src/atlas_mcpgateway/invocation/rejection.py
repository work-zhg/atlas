"""拒绝：原因、给人看的说明、有没有转发给上游。

准入阶段的拒绝回 HTTP 状态码和 JSON-RPC 错误体；调用阶段的拒绝是 JSON-RPC 错误（HTTP 200）。
error.data 带 reason 和 forwarded —— Atlas 按 reason 分支（Key 失效重取一次，服务停用、工具不存在
在本段记住），靠 forwarded=false 判断能不能安全重试。★ reason 改名就是改协议。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

__all__ = ["Reason", "Rejection"]


class Reason(StrEnum):
    SERVICE_NOT_FOUND = "service_not_found"
    SERVICE_DISABLED = "service_disabled"
    KEY_MISSING = "key_missing"
    KEY_INVALID = "key_invalid"
    FORBIDDEN = "forbidden"
    AUTHZ_UNAVAILABLE = "authz_unavailable"
    TOOL_UNKNOWN = "tool_unknown"
    INVALID_PARAMS = "invalid_params"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_ERROR = "upstream_error"


#: 原因 → (HTTP 状态码, JSON-RPC 错误码)。错误码在 JSON-RPC 留给服务端的区间里（-32602 是标准的）
_CODES: dict[Reason, tuple[int, int]] = {
    Reason.SERVICE_NOT_FOUND: (404, -32040),
    Reason.SERVICE_DISABLED: (403, -32041),
    Reason.KEY_MISSING: (401, -32042),
    Reason.KEY_INVALID: (401, -32043),
    Reason.FORBIDDEN: (403, -32044),
    Reason.AUTHZ_UNAVAILABLE: (503, -32045),
    Reason.TOOL_UNKNOWN: (200, -32046),
    Reason.INVALID_PARAMS: (200, -32602),
    Reason.UPSTREAM_UNAVAILABLE: (200, -32050),
    Reason.UPSTREAM_ERROR: (200, -32051),
}


class Rejection(Exception):
    def __init__(
        self, reason: Reason, message: str, *, forwarded: bool = False, **extra: Any
    ) -> None:
        super().__init__(message)
        self.reason, self.message, self.forwarded, self.extra = reason, message, forwarded, extra

    @property
    def code(self) -> int:
        return _CODES[self.reason][1]

    @property
    def http_status(self) -> int:
        return _CODES[self.reason][0]

    def error(self, service: str) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "data": {
                "reason": self.reason.value,
                "forwarded": self.forwarded,
                "service": service,
                **self.extra,
            },
        }
