"""acp 会话的 MCP 下发（MCP 方案设计 §7）。

ACP Agent 的 MCP client 是会话 Pod 里的 CLI：Atlas 只在建会话时把服务的**网关地址**和这一轮
权限主体的 **API Key** 交给它，之后的握手、拉工具列表、调用都由 CLI 自己完成，Atlas 拦截不到
单次调用 —— MCP 网关是唯一的治理点，它对每个请求认人、判权、按工具执行复核。

  · **粒度只能到服务**：ACP 的 mcpServers 是服务级的，CLI 自己 tools/list。复核由网关按工具
    执行：待复核 / 被拒绝的工具从 CLI 拿到的列表里消失、调用被拒，其余照常（§4.4、§7.1）
  · **会话内不变**：只在 session.open 时下发；attach 接上的会话沿用当时那份。收回授权、停用
    服务、拒绝工具定义仍然对已打开的会话生效（网关每个请求都判）
  · **主体 Key 进 Pod**：有意接受的风险（§7.2），Key 的能力不超出主体的授权
  · 没权限、没 Key 的服务不下发，并说明原因（与 Native 的装配同一套规则）
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from ...domain.events import EventType
from ...domain.mcp_naming import parse_tool_id
from .catalog import McpCatalog, McpUnavailable
from .gateway import SubjectAccess

__all__ = ["acp_mcp_servers"]


async def acp_mcp_servers(
    tool_names: tuple[str, ...] | list[str],
    catalog: McpCatalog,
    *,
    user_id: UUID,
    digests: Mapping[str, str],
    drift_policy: str,
    access: SubjectAccess,
) -> tuple[list[dict[str, Any]], list[tuple[EventType, dict[str, Any]]]]:
    """→ (session.open 的 mcpServers, 要告诉用户的 notices)。

    access：这一轮的权限主体能用什么（服务 + Key），建会话前问过网关。
    """
    wanted: dict[str, list[str]] = {}
    for name in tool_names:
        if parsed := parse_tool_id(name):
            wanted.setdefault(parsed[0], []).append(parsed[1])

    servers: list[dict[str, Any]] = []
    notices: list[tuple[EventType, dict[str, Any]]] = []
    for name in sorted(wanted):
        config = catalog.server(name)
        if config is None:
            raise McpUnavailable(f"MCP 服务 {name!r} 未在 MCP 网关注册或已停用")
        if (skipped := _skip_reason(name, access)) is not None:
            notices.append((EventType.MCP_TOOL_SKIPPED, skipped))
            continue
        snap = await catalog.snapshot(name)

        blocked = False
        for tool_name in wanted[name]:
            tool = snap.tool(tool_name)
            ident = f"mcp:{name}:{tool_name}"
            recorded = digests.get(ident)
            if tool is not None and recorded and recorded != tool.digest:
                action = "blocked" if drift_policy == "block" else "used"
                blocked = blocked or action == "blocked"
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
        if blocked:
            # CLI 看得到服务的全部工具：block 策略下有一个定义变了，整个服务这一轮不下发
            notices.append(
                (
                    EventType.MCP_TOOL_DRIFT,
                    {"tool": f"mcp:{name}:*", "server": name, "action": "blocked"},
                )
            )
            continue

        assert access.key is not None  # _skip_reason 已拦下没 Key 的
        servers.append(
            {
                "type": "http",
                "name": name,
                # 网关上的地址：Pod 看不到 Higress
                "url": config.url,
                # ★ ACP 的 headers 是数组，不是对象
                "headers": [
                    {"name": "Authorization", "value": f"Bearer {access.key}"},
                    # 使用者：只作审计，网关照转；权限主体由网关按 Key 写入
                    {"name": "X-Uid", "value": str(user_id)},
                ],
            }
        )
    return servers, notices


def _skip_reason(server: str, access: SubjectAccess) -> dict[str, Any] | None:
    """没权限、没 Key 的服务不下发，并说明原因（§7.1，与 Native 的装配同一套规则）。"""
    if access.allows(server) and access.key is not None:
        return None
    reason = "forbidden" if not access.allows(server) else "no_key"
    message = (
        f"权限主体没有被授权使用 MCP 服务 {server}"
        if reason == "forbidden"
        else "权限主体还没有 MCP API Key"
    )
    return {
        "server": server,
        "tools": [f"mcp:{server}:*"],
        "reason": reason,
        "subject": access.subject,
        "message": f"{message}，本会话不下发；请联系服务所有者或 MCP 管理员",
    }
