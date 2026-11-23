"""上游接入（c4-model/mcpgateway/code-upstream.html）：网关直接连的服务。

    gateway.py      UpstreamGateway：唯一入口，按服务类型选上游，注入上游凭据
    spec.py         值对象：UpstreamSpec、ToolBinding、UpstreamAuth、Discovered
    base.py         扩展点 Upstream：发现工具、调用工具
    mcp.py          MCP 代理：已有的远程 MCP Server（Streamable HTTP / 旧版 SSE），网关是客户端
    rest.py         REST 转换：解析 OpenAPI、按映射拼 HTTP 请求
    grpc.py         gRPC 转换：服务端反射 / 描述文件，动态构造请求消息
    credentials.py  上游凭据、OAuth2 Token
    guard.py        上游地址不能指向平台内部

★ 只收值对象，不认识任何领域：服务目录把服务、工具转成 UpstreamSpec、ToolBinding 交过来，身份是
  工具调用算好的请求头。
★ 上游凭据由网关注入（credentials.py），调用方拿不到；调用方的 Key 不转发。
★ 不设读超时：工具调用可能要很久。超时属于流量治理（通用组件，单独设计）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from opentelemetry import propagate

__all__ = [
    "DiscoveryFailed",
    "InvalidArguments",
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


class InvalidArguments(Exception):
    """参数转不成上游的请求（gRPC：字段不存在、类型不对）。请求没有发出去。"""


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
