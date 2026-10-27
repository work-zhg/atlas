"""Atlas 的 MCP 接入（对应 atlas-server 的 providers/mcp）：只连 MCP 网关，不知道 Higress。

    metadata        元数据缓存：从 MCP 网关读服务、工具定义、复核结论
                    （Atlas 里在 Redis，多副本共享）。不自己连 MCP 服务发现工具
    AtlasMcp
      editor_tools  编辑器：只列主体能用的服务里、复核通过的工具（问网关主体能用哪些服务）
      assemble      Native 装配：问网关主体能用哪些服务、取主体 Key → 逐个服务 / 工具过闸门
                    （无权限 / 无 Key / 未复核 → 跳过并提示）→ 读缓存造 ToolSpec，不连网
      acp_servers   ACP 下发：同样的过滤，按服务下发网关上的地址 + 主体 Key
    SessionPool     一个 Run 的会话池：第一次用到某个服务才建会话，Run 结束统一关

★ ToolSpec.call 是这里的代码：持有会话 → FastMCP 的 tools/call → MCP 结果转成 ToolResult。
  engine 只看到名字、描述、参数定义和一个异步函数，不知道背后是 MCP。
★ 主体 Key 和身份请求头写在 FastMCP 客户端的传输层，不进工具参数：模型换不了身份。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import httpx
from atlas_engine import ToolResult, ToolSpec
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from hops import hop
from mcp.types import TextContent

STATUS_TEXT = {
    "pending_review": "工具定义待复核",
    "rejected": "工具定义被拒绝",
    "unusable": "工具定义不可用",
}


def parse(tool_names: list[str]) -> dict[str, list[str]]:
    """Agent 配置里的 mcp:服务:工具 → {服务: [工具]}；ACP 按服务配，工具写 *。"""
    wanted: dict[str, list[str]] = defaultdict(list)
    for ref in tool_names:
        _, service, tool = ref.split(":")
        wanted[service].append(tool)
    return dict(wanted)


class SessionPool:
    """FastMCP 客户端可重入、按引用计数：持有着，之后的调用就复用这个会话。"""

    def __init__(self) -> None:
        self.clients: dict[str, Client[Any]] = {}
        self._held: list[str] = []
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def hold(self, service: str) -> Client[Any]:
        async with self._locks[service]:  # 同一步并发的 tool_call，同一个服务只建一次会话
            if service not in self._held:
                hop("Atlas 会话池", f"{service} 第一次被调用 → 建会话（本 Run 内复用）")
                await self.clients[service].__aenter__()
                self._held.append(service)
        return self.clients[service]

    async def aclose(self) -> None:
        for service in reversed(self._held):
            await self.clients[service].__aexit__(None, None, None)
        if self._held:
            hop("Atlas 会话池", f"Run 结束，关会话 {self._held}")
        self._held.clear()


@dataclass
class Assembly:
    tools: list[ToolSpec]
    notices: list[str]
    pool: SessionPool = field(default_factory=SessionPool)


class AtlasMcp:
    def __init__(self, gateway_url: str) -> None:
        self.gateway = gateway_url
        #: 元数据缓存：服务名 → {title, enabled, tools: [...]}
        self.metadata: dict[str, dict[str, Any]] = {}

    async def _get(self, path: str) -> httpx.Response:
        async with httpx.AsyncClient() as http:
            return await http.get(f"{self.gateway}{path}")

    async def refresh_metadata(self) -> None:
        services = (await self._get("/internal/metadata")).json()["services"]
        self.metadata = {s["name"]: s for s in services}
        hop("Atlas", f"元数据缓存已刷新：{list(self.metadata)}")

    async def services_of(self, subject: str) -> set[str]:
        return set((await self._get(f"/internal/subjects/{subject}/services")).json()["services"])

    async def key_of(self, subject: str) -> str | None:
        r = await self._get(f"/internal/subjects/{subject}/key")
        return r.json()["key"] if r.status_code == 200 else None

    def _tool(self, service: str, name: str) -> dict[str, Any] | None:
        return next((t for t in self.metadata[service]["tools"] if t["name"] == name), None)

    # ------------------------------------------------------------ 编辑器

    async def editor_tools(self, subject: str) -> tuple[dict[str, list[str]], int]:
        """→ ({服务: [可选的工具]}, 没权限的服务个数)。"""
        usable = await self.services_of(subject)
        listed = {
            s: [t["name"] for t in m["tools"] if t["status"] == "available"]
            for s, m in self.metadata.items()
            if s in usable
        }
        hidden = sum(1 for s, m in self.metadata.items() if m["enabled"] and s not in usable)
        return listed, hidden

    # ------------------------------------------------------------ Native 装配

    async def assemble(
        self, tool_names: list[str], *, subject: str, user: str, run_id: str
    ) -> Assembly:
        wanted = parse(tool_names)
        if not wanted:
            return Assembly([], [])  # 没用 MCP 的 Agent 不问网关
        usable, key = await self.services_of(subject), await self.key_of(subject)
        asm = Assembly([], [])
        for service, names in wanted.items():
            if service not in usable:
                asm.notices.append(f"跳过 {service}：{subject} 没有被授权使用这个 MCP 服务")
                continue
            if key is None:
                asm.notices.append(f"跳过 {service}：{subject} 还没有 API Key")
                continue
            # 一个 Run × 服务一个客户端；Key 与身份请求头写在传输层
            transport = StreamableHttpTransport(
                f"{self.gateway}/mcp/{service}",
                headers={"Authorization": f"Bearer {key}", "X-Uid": user, "X-Run-Id": run_id},
            )
            asm.pool.clients[service] = Client(transport, mode="legacy")
            for name in names:
                meta = self._tool(service, name)
                if meta is None or meta["status"] != "available":
                    why = (
                        STATUS_TEXT.get(meta["status"], meta["status"]) if meta else "没有这个工具"
                    )
                    asm.notices.append(f"跳过 {service}.{name}：{why}")
                    continue
                asm.tools.append(self._spec(service, meta, asm.pool))
        return asm

    def _spec(self, service: str, meta: dict[str, Any], pool: SessionPool) -> ToolSpec:
        wire_name = meta["name"]

        async def call(arguments: dict[str, Any]) -> ToolResult:
            client = await pool.hold(service)
            result = await client.call_tool(wire_name, arguments, raise_on_error=False)
            text = "\n".join(c.text for c in result.content if isinstance(c, TextContent))
            return ToolResult(text=text, is_error=result.is_error)

        return ToolSpec(
            name=f"{service}__{wire_name}",  # 模型看到的名字；发给服务的仍是线上名
            description=meta["description"],
            input_schema=meta["input_schema"],  # 来自网关库里那一份（读缓存，不连网）
            call=call,
            read_only=meta["read_only"],
        )

    # ------------------------------------------------------------ ACP 下发

    async def acp_servers(
        self, tool_names: list[str], *, subject: str, user: str
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """→ (session/new 的 mcpServers, 提示)。

        按服务下发：CLI 自己拉列表，工具级的复核由网关执行。
        """
        usable, key = await self.services_of(subject), await self.key_of(subject)
        servers, notices = [], []
        for service in parse(tool_names):
            if service not in usable or key is None:
                why = "没有权限" if service not in usable else "没有 Key"
                notices.append(f"不下发 {service}：{why}")
                continue
            servers.append(
                {
                    "type": "http",
                    "name": service,
                    "url": f"{self.gateway}/mcp/{service}",  # 网关上的地址，看不到 Higress
                    "headers": [
                        {"name": "Authorization", "value": f"Bearer {key}"},
                        {"name": "X-Uid", "value": user},
                    ],
                }
            )
        return servers, notices
