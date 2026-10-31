"""上游：网关直接连的服务（没有 Higress）。

    mcp.py    MCP 代理：已有的远程 MCP Server（Streamable HTTP / 旧版 SSE），网关作为客户端调它
    rest.py   REST 转换：解析 OpenAPI、按映射拼 HTTP 请求
    grpc.py   gRPC 转换：服务端反射 / 描述文件，动态构造请求消息

★ 调用方只认网关：网关自己就是 MCP 服务端（dataplane.py），三种上游都由它转换后调用。
★ 上游凭据由网关注入（credentials.py），调用方拿不到；调用方的 Key 不转发。
★ 不设读超时：工具调用可能要很久。超时属于流量治理（通用组件，单独设计）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import propagate

__all__ = [
    "CallContext",
    "DiscoveryFailed",
    "UpstreamError",
    "UpstreamUnavailable",
    "describe_error",
    "root_causes",
    "tool_result",
    "trace_headers",
]


class UpstreamUnavailable(Exception):
    """上游连不上（或在给出结果之前断开）：请求没有被执行，或者不知道有没有被执行。"""


class UpstreamError(Exception):
    """上游 MCP Server 回了 JSON-RPC 错误（不是工具执行失败，工具失败是 isError 的结果）。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class DiscoveryFailed(Exception):
    """拉取 / 解析接口定义失败：给管理员看的原因。"""


@dataclass(frozen=True)
class CallContext:
    """一次调用里要带给上游的身份与追踪上下文。

    key_uid    Key 在用户中心的主体（权限主体）→ X-Auth-Uid
    user_uid   使用者：用户 Key 是持有人；智能体 Key 是调用方带来的（只作审计，网关不校验）→ X-Uid
    audit      调用方带来的 X-Run-Id、X-Thread-Id（只作审计）→ 原样转给上游
    """

    key_uid: str
    user_uid: str | None = None
    pass_identity: bool = True
    #: 调用方带来的审计头（X-Run-Id、X-Thread-Id），照转给上游
    audit: Mapping[str, str] = field(default_factory=dict)

    def identity(self) -> dict[str, str]:
        if not self.pass_identity:
            return {}
        return {"X-Auth-Uid": self.key_uid, "X-Uid": self.user_uid or self.key_uid, **self.audit}


def trace_headers() -> dict[str, str]:
    """当前追踪上下文 → W3C traceparent / tracestate（发给上游）。"""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def tool_result(
    text: str, *, structured: Mapping[str, Any] | None = None, is_error: bool = False
) -> dict[str, Any]:
    """MCP 线上格式的 tools/call 结果。"""
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None:
        out["structuredContent"] = dict(structured)
    return out


def root_causes(exc: BaseException) -> list[BaseException]:
    """真正的原因：ExceptionGroup 展开到叶子，再顺着显式的 __cause__ 往下走
    （FastMCP 把连接失败包成 RuntimeError … from ConnectError）。

    ★ cause 是 CancelledError 时停下：asyncio.timeout 抛的 TimeoutError 就是 from CancelledError。
    """
    if isinstance(exc, BaseExceptionGroup):
        return [root for sub in exc.exceptions for root in root_causes(sub)]
    cause = exc.__cause__
    if cause is not None and not isinstance(cause, asyncio.CancelledError):
        return root_causes(cause)
    return [exc]


def describe_error(exc: BaseException) -> str:
    root = root_causes(exc)[0]
    text = str(root) or type(root).__name__
    return f"{type(root).__name__}: {text}"[:500]
