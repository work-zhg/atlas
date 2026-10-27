"""ACP 会话 Pod 里的 CLI（mock）：拿 Atlas 下发的 mcpServers，自己连 MCP 网关。

Atlas 拦截不到这里的单次调用，MCP 网关是唯一的治理点。这里演示四件事：
    ① 正常用：经网关拉工具列表（未复核的工具已被网关去掉）、调工具
    ② 调一个列表里没有的工具（缓存了旧列表，或者 Pod 里的代码硬调）→ 网关拒绝
    ③ Pod 里的代码伪造 X-Auth-Uid → 网关丢掉，后端看到的仍是 Key 认出的主体
    ④ Pod 里的代码在集群里找到 Higress，绕过网关直连 → Higress 拒绝（没有网关凭据）
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from hops import hop


def _client(server: dict[str, Any], **extra: str) -> Client[Any]:
    headers = {h["name"]: h["value"] for h in server["headers"]} | extra
    return Client(StreamableHttpTransport(server["url"], headers=headers), mode="legacy")


async def run(mcp_servers: list[dict[str, Any]], *, higress_found_in_cluster: str) -> None:
    by_name = {s["name"]: s for s in mcp_servers}
    hop("Pod CLI", "① 连上下发的每个服务，拉工具列表")
    async with AsyncExitStack() as stack:
        clients = {n: await stack.enter_async_context(_client(s)) for n, s in by_name.items()}
        for name, client in clients.items():
            hop("Pod CLI", f"{name} 的工具：{[t.name for t in await client.list_tools()]}")
        result = await clients["weather"].call_tool("get_weather", {"city": "北京"})
        hop("Pod CLI", f"调 weather.get_weather → {result.content[0].text}")

        hop("Pod CLI", "② 硬调列表里没有的 ticket.delete_ticket")
        try:
            await clients["ticket"].call_tool("delete_ticket", {"ticket_id": "T-001"})
        except Exception as exc:
            hop("Pod CLI", f"被拒：{exc}")

    hop("Pod CLI", "③ 伪造 X-Auth-Uid=u-admin 调 ticket.create_ticket")
    async with _client(by_name["ticket"], **{"X-Auth-Uid": "u-admin"}) as forged:
        result = await forged.call_tool("create_ticket", {"title": "伪造身份试试"})
    hop("Pod CLI", f"结果：{result.content[0].text}")

    hop("Pod CLI", "④ 绕过网关，直连集群里的 Higress（只有主体 Key，没有网关凭据）")
    key = next(h["value"] for h in by_name["ticket"]["headers"] if h["name"] == "Authorization")
    async with httpx.AsyncClient() as http:
        r = await http.post(
            f"{higress_found_in_cluster}/mcp-servers/ticket/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": key, "Accept": "application/json, text/event-stream"},
        )
    hop("Pod CLI", f"直连 Higress → HTTP {r.status_code} {r.text}")
