"""ACP v1 定义的错误码（schema 的 ErrorCode，JSON-RPC 标准码之外的部分）。"""

from __future__ import annotations

__all__ = ["AcpErrorCode"]


class AcpErrorCode:
    REQUEST_CANCELLED = -32800  # $/cancel_request 或内部取消
    AUTH_REQUIRED = -32000  # 需要先鉴权
    RESOURCE_NOT_FOUND = -32002  # 资源（如会话）不存在
