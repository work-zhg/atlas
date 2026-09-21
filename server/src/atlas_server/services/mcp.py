"""MCP 工具接入（文档 §4.6 / §11 / §14）。

三条来自决策的边界：

1. **只做 HTTP/SSE 远程传输**，不做 stdio。
   stdio 要在进程内执行器里管子进程的启停与回收，与「重启会中断 run」
   的既有代价叠加，运维复杂度明显上升。远程只是发请求。

2. **凭据用 ${ENV} 占位符**，真值在服务器环境变量里。
   §14 要求凭据不进 `agent_version.spec` —— spec 会被 API 原样返回给前端，
   写进去等于公开。这里连配置本身都不存密文：只存占位符，用时才解析。

3. **engine 不认识 MCP**。这里把工具解析成 BaseTool 交给
   `runner.run(extra_tools=...)`，engine 只当作普通工具用。

工具命名：`mcp:<server>:<tool>`，与内置工具区分开，前缀也让
`unsupported_tools` 知道该放行（可用性由本模块判定）。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MCP_PREFIX = "mcp:"

#: ${VAR} 占位符
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class McpServerConfig(BaseModel):
    """一个远程 MCP server。

    headers 里可以写 `{"Authorization": "Bearer ${MY_TOKEN}"}` ——
    真值从环境变量取，配置本身不含密文。
    """

    name: str = Field(min_length=1, max_length=64)
    url: str
    transport: str = "streamable_http"
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class MissingTool(RuntimeError):
    """spec 里勾了某个 MCP 工具，但目标 server 上没有它。"""


class MissingCredential(RuntimeError):
    """占位符引用的环境变量不存在。

    ★ 不能静默把它当空串：那会用一个空 token 去连，服务端返回 401，
      报错指向「认证失败」而不是「你没配这个环境变量」（§13.2）。
    """


def resolve_env_refs(value: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        var = match.group(1)
        found = os.environ.get(var)
        if found is None:
            raise MissingCredential(f"环境变量 {var} 未设置（被 MCP 配置引用）")
        return found

    return _ENV_REF.sub(_sub, value)


def tool_id(server: str, tool: str) -> str:
    return f"{MCP_PREFIX}{server}:{tool}"


def parse_tool_id(value: str) -> tuple[str, str] | None:
    """`mcp:<server>:<tool>` → (server, tool)。不是 MCP 工具则返回 None。"""
    if not value.startswith(MCP_PREFIX):
        return None
    rest = value[len(MCP_PREFIX) :]
    server, _, tool = rest.partition(":")
    return (server, tool) if server and tool else None


class McpService:
    """连接远程 MCP server、列举与加载工具。

    每次调用都新建 client：MCP 会话是有状态的，跨 run 复用会让一个 run 的
    失败影响到另一个。远程调用的建连开销相对模型调用可以忽略。
    """

    def __init__(self, servers: list[McpServerConfig]) -> None:
        self._servers = [s for s in servers if s.enabled]

    @property
    def configured(self) -> bool:
        return bool(self._servers)

    def _connections(self, only: set[str] | None = None) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for server in self._servers:
            if only is not None and server.name not in only:
                continue
            out[server.name] = {
                "url": resolve_env_refs(server.url),
                "transport": server.transport,
                "headers": {k: resolve_env_refs(v) for k, v in server.headers.items()},
            }
        return out

    async def list_tools(self) -> list[dict[str, Any]]:
        """给 `/v1/tools` 用的目录。

        单个 server 连不上不该让整张目录挂掉 —— 其余 server 的工具照常返回，
        失败的那个在日志里留痕。
        """
        catalog: list[dict[str, Any]] = []
        for server in self._servers:
            try:
                tools = await self._load(only={server.name})
            except Exception as exc:
                logger.warning("MCP server %s 不可用：%s", server.name, exc)
                continue
            catalog.extend(
                {
                    "name": tool_id(server.name, t.name),
                    "display_name": t.name,
                    "description": (t.description or "")[:500],
                    "server": server.name,
                }
                for t in tools
            )
        return catalog

    async def tools_for(self, requested: list[str]) -> list[Any]:
        """按 spec.tool_names 里的 `mcp:*` 条目加载真实工具对象。

        ★ 逐 server 加载而不是一次性加载再按名字过滤：MultiServerMCPClient
          返回的工具对象上没有 server 归属信息，混在一起后同名工具就分不清
          来自哪个 server 了。逐个来，归属由调用方自己掌握。
        """
        wanted: dict[str, set[str]] = {}
        for name in requested:
            parsed = parse_tool_id(name)
            if parsed is None:
                continue
            server, tool = parsed
            wanted.setdefault(server, set()).add(tool)

        out: list[Any] = []
        for server, names in wanted.items():
            loaded = await self._load(only={server})
            picked = {t.name for t in loaded}
            if missing := names - picked:
                # 用户勾了、server 上却没有 —— 明确报出来而不是少装几个工具
                # 就默默跑（§13.2）
                raise MissingTool(
                    f"MCP server {server!r} 上不存在工具：{sorted(missing)}",
                )
            out.extend(t for t in loaded if t.name in names)
        return out

    async def _load(self, *, only: set[str] | None = None) -> list[Any]:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        connections = self._connections(only)
        if not connections:
            return []
        client = MultiServerMCPClient(connections)  # type: ignore[arg-type]
        return await client.get_tools()
