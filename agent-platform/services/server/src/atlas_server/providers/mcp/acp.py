"""acp 会话的 MCP 下发（技能 / MCP 设计 §10）。

此前 session.open 的 mcpServers 固定为 []：acp agent 上配的 mcp:* 工具保存成功、
运行时静默不生效。

三个与 native 不同的地方：
  · **粒度只能到 server**：ACP 的 mcpServers 是 server 级的，CLI 自己 tools/list，
    看得到该 server 的全部工具。所以复核闸门也只能按 server 判 —— 有任何一个
    工具未复核 / 被拒，整个 server 不下发。
  · **会话内不变**：只在 session.open 时下发；attach 接上的会话沿用当时那份。
  · **真凭据进 Pod**：解析后的 token 交给 CLI 进程（P3 换成网关会话令牌）。
    所以只下发 platform 作用域的 server。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from ...domain.events import EventType
from ...domain.mcp_naming import parse_tool_id
from .catalog import McpUnavailable, SettingsCatalog
from .config import connection_for
from .review import ReviewIndex, review_status

__all__ = ["acp_mcp_servers"]


async def acp_mcp_servers(
    tool_names: tuple[str, ...] | list[str],
    catalog: SettingsCatalog,
    *,
    user_id: UUID,
    digests: Mapping[str, str],
    drift_policy: str,
    reviews: ReviewIndex | None,
    timeout_s: float,
) -> tuple[list[dict[str, Any]], list[tuple[EventType, dict[str, Any]]]]:
    """→ (session.open 的 mcpServers, 要告诉用户的 notices)。"""
    wanted: dict[str, list[str]] = {}
    for name in tool_names:
        if parsed := parse_tool_id(name):
            wanted.setdefault(parsed[0], []).append(parsed[1])

    servers: list[dict[str, Any]] = []
    notices: list[tuple[EventType, dict[str, Any]]] = []
    for name in sorted(wanted):
        config = catalog.server(name)
        if config is None:
            raise McpUnavailable(f"MCP server {name!r} 未注册或已禁用")
        if config.credential_scope != "platform":
            raise McpUnavailable(
                f"MCP server {name!r} 需要每个用户自己授权，acp 会话暂不支持这类 server"
            )
        snap = await catalog.snapshot(name)

        blocked: str | None = None
        for tool in snap.tools:
            status = review_status(config, tool, reviews)
            if status in ("pending_review", "rejected"):
                blocked = status
                break
        for tool_name in wanted[name]:
            tool = snap.tool(tool_name)
            ident = f"mcp:{name}:{tool_name}"
            recorded = digests.get(ident)
            if tool is not None and recorded and recorded != tool.digest:
                action = "blocked" if drift_policy == "block" else "used"
                if action == "blocked":
                    blocked = blocked or "blocked"
                notices.append(
                    (
                        EventType.MCP_TOOL_DRIFT,
                        {
                            "tool": ident,
                            "server": name,
                            "old_digest": recorded,
                            "new_digest": tool.digest,
                            "action": action,
                        },
                    )
                )
        if blocked is not None:
            notices.append(
                (
                    EventType.MCP_TOOL_DRIFT,
                    {"tool": f"mcp:{name}:*", "server": name, "action": blocked},
                )
            )
            continue

        conn = connection_for(config, timeout_s=timeout_s)  # 缺环境变量明确报错
        servers.append(
            {
                "type": "sse" if config.transport == "sse" else "http",
                "name": name,
                "url": conn["url"],
                # ★ ACP 的 headers 是数组，不是对象
                "headers": [{"name": k, "value": v} for k, v in conn["headers"].items()]
                + [{"name": "X-Atlas-User", "value": str(user_id)}],
            }
        )
    return servers, notices
