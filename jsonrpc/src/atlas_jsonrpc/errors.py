"""JSON-RPC 2.0 的错误码与异常。"""

from __future__ import annotations

from typing import Any

__all__ = ["ChannelClosed", "ErrorCode", "RpcFault"]


class ErrorCode:
    """JSON-RPC 2.0 规范定义的标准错误码。

    应用自己的错误码放在 -32000 ~ -32099（规范留给实现定义的区段），
    由使用方（ACP 模型、上游协议）各自定义，不放在这里。
    """

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603


class RpcFault(Exception):
    """一个 JSON-RPC 错误。

    两个方向都用它：
      · 请求处理函数抛出它 → 端点把它编码成错误响应回给对方
      · 对方回了错误响应 → ``RpcEndpoint.request`` 抛出它
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


class ChannelClosed(Exception):
    """通道已关闭：挂起的请求再也等不到响应，之后的发送也不会送达。"""
