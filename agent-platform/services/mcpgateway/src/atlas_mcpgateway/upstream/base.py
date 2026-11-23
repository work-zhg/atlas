"""扩展点 Upstream：一种上游要实现的两个动作。

加一种上游（例如 GraphQL）只加一个实现、在 UpstreamGateway 里登记；服务目录和工具调用都不按服务
类型分支。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .spec import Discovered, ToolBinding, UpstreamAuth, UpstreamSpec

__all__ = ["Upstream"]


class Upstream(Protocol):
    async def discover(self, spec: UpstreamSpec, auth: UpstreamAuth) -> Discovered:
        """拉不到、解析不了抛 DiscoveryFailed（原因给管理员看）。"""
        ...

    async def call(
        self,
        spec: UpstreamSpec,
        binding: ToolBinding,
        arguments: Mapping[str, Any],
        auth: UpstreamAuth,
        identity: Mapping[str, str],
    ) -> dict[str, Any]:
        """→ MCP 线上格式的 CallToolResult。连不上抛 UpstreamUnavailable，上游回 JSON-RPC 错误抛
        UpstreamError，参数转不成请求抛 InvalidArguments；工具自己失败是 isError 的结果。"""
        ...
