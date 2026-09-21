import type { AgentStatus } from "@/api/types";

/**
 * 智能体状态的展示口径，列表页与编辑器共用。
 *
 * 放在这里而不是各自定义：两处一旦分叉，用户会在列表看到"草稿"是灰的、
 * 进编辑器又变成橙的，还得自己判断是不是同一个状态。
 */
export const AGENT_STATUS_META: Record<AgentStatus, { color: string; label: string }> = {
  enabled: { color: "success", label: "已启用" },
  draft: { color: "default", label: "草稿" },
  archived: { color: "warning", label: "已归档" },
};
