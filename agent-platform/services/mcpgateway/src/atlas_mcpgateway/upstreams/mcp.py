"""MCP 代理：已有的远程 MCP Server，网关作为客户端调它。

    传输   streamable = Streamable HTTP；sse = 旧版 HTTP+SSE（2024-11-05）
    定义   tools/list（含分页）→ MCP 线上格式的工具定义，存库（registry.refresh）
    调用   每次 tools/call 开一个上游会话：initialize → tools/call → 关闭

★ 每次调用一个上游会话：网关对下游是无状态的（任一实例处理任一请求），不在实例里留上游会话。
  一次调用 = initialize + initialized + tools/call 三个请求；多出来的握手是已知开销，
  以后按需要做连接池。
★ mode="legacy"：只用 initialize 握手。几乎所有 MCP Server 都支持，新版发现流程不是都支持。
★ 追踪上下文：MCP SDK 会写进 params._meta；HTTP 头也带一份（有的服务只认头）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from mcp.shared.exceptions import MCPError

from . import UpstreamError, UpstreamUnavailable, describe_error, root_causes, trace_headers

__all__ = ["TRANSPORTS", "McpUpstream"]

TRANSPORTS = ("streamable", "sse")


class McpUpstream:
    def __init__(self, url: str, *, transport: str, headers: Mapping[str, str]) -> None:
        self.url = url
        self.transport = transport
        self.headers = dict(headers)

    def _transport(self, extra: Mapping[str, str] | None = None) -> Any:
        from fastmcp.client.transports import SSETransport, StreamableHttpTransport

        headers = {**self.headers, **(extra or {})}
        if self.transport == "sse":
            return SSETransport(self.url, headers=headers)
        return StreamableHttpTransport(self.url, headers=headers)

    async def list_tools(self, *, timeout_s: float) -> list[dict[str, Any]]:
        """拉一次完整的工具列表。★ 分页上限 100 页：超过即报错（保留旧定义），不截断成半份目录。"""
        from fastmcp import Client

        try:
            async with asyncio.timeout(timeout_s):
                async with Client(self._transport(), mode="legacy") as client:
                    tools = await client.list_tools(max_pages=100)
        except Exception as exc:
            raise UpstreamUnavailable(describe_error(exc)) from exc
        return [t.model_dump(by_alias=True, exclude_none=True) for t in tools]

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any], *, identity: Mapping[str, str]
    ) -> dict[str, Any]:
        """→ MCP 线上格式的 CallToolResult。上游回 JSON-RPC 错误抛 UpstreamError，连不上抛
        UpstreamUnavailable。工具执行失败是 isError 的结果，原样返回。"""
        import mcp.types as mt
        from fastmcp import Client

        transport = self._transport({**identity, **trace_headers()})
        request = mt.CallToolRequest(
            params=mt.CallToolRequestParams(name=name, arguments=dict(arguments))
        )
        try:
            async with Client(transport, mode="legacy") as client:
                # ★ 直接发 tools/call，不走 call_tool：它要按输出定义校验结果，缓存里没有就先拉一次
                #   tools/list（每次调用多一个请求）。校验是最终调用方的事，网关只转发
                result = await client.session.send_request(request, mt.CallToolResult)
        except Exception as exc:
            error = next((e for e in root_causes(exc) if isinstance(e, MCPError)), None)
            if error is not None:
                raise UpstreamError(
                    error.error.code, error.error.message, error.error.data
                ) from exc
            raise UpstreamUnavailable(describe_error(exc)) from exc
        out = result.model_dump(by_alias=True, exclude_none=True)
        out.pop("_meta", None)
        out.pop("resultType", None)
        return out
