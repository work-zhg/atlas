"use client";

/**
 * 服务详情 · 授权（MCP 方案设计 §5.3、§9.1）：谁能用这个 MCP 服务 = 用户中心的数据权限
 * （数据编码 McpServer，属于 MCP 网关这个应用）。界面见 GrantsPanel（和模型共用）。
 *
 * ★ 有授权还要有 API Key 才调得了：授完到「API Key」页给没有 Key 的人发放。
 */
import { useGwGrantActions, useGwGrants } from "@/api/mcp-gateway";
import { GrantsPanel } from "./GrantsPanel";

export function McpGrants({ service, canGrant }: { service: string; canGrant: boolean }) {
  const grantsQ = useGwGrants(service);
  const { add, revoke } = useGwGrantActions(service);
  return (
    <GrantsPanel
      grants={grantsQ}
      add={add}
      revoke={revoke}
      canGrant={canGrant}
      agentHint="以 Agent 为权限的 Agent 按它的数字员工判：能用这个 Agent 的人都能经它调用这个服务。数字员工还要有 API Key。"
    />
  );
}
