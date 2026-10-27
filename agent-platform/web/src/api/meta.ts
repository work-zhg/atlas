"use client";

import { useQuery } from "@tanstack/react-query";

import { http } from "@/lib/http";
import { qk } from "./keys";
import type { ModelInfo, ToolInfo } from "./types";

interface ModelListResponse {
  data: ModelInfo[];
  gateway_reachable: boolean;
}

/**
 * 模型目录 —— 编辑器据此决定渲染 effort 分段器还是 temperature 滑块（§3 D3）。
 * 每个模型收不收哪些参数是实测落库的，前端不要自己硬编码模型名判断。
 */
export function useModels() {
  return useQuery({
    queryKey: qk.models,
    queryFn: () => http.get<ModelListResponse>("/v1/models"),
    // 目录变动极少，且后端已有 300s 缓存；前端再缓 5 分钟避免每次开编辑器都打网关
    staleTime: 5 * 60_000,
  });
}

/** 编辑器里 MCP 工具按谁的权限列出（经 MCP 网关时才有，doc/mcp-design.html §9.2） */
export interface McpSubjectInfo {
  /** user = 以人为权限（当前用户）；agent = 以 Agent 为权限（它的数字员工） */
  kind: "user" | "agent";
  /** 以 Agent 为权限、还没有数字员工（没保存过）时为空 */
  uid: string | null;
  /** active | disabled | none */
  key_status: string;
  /** 已启用、但权限主体没有权限的服务个数（不列出它们） */
  hidden_servers: number;
  notes: string[];
}

export interface ToolListResponse {
  data: ToolInfo[];
  mcp_subject?: McpSubjectInfo | null;
}

/**
 * 工具目录。MCP 工具按这个 Agent 的权限主体列出：编辑已有的给 agentId，新建的给 authSubject
 * （以人为权限 = 按你自己的权限；以 Agent 为权限 = 按它的数字员工，没保存过就还没有）。
 */
export function useTools(ctx: { agentId?: string; authSubject?: "user" | "agent" } = {}) {
  return useQuery({
    queryKey: [...qk.tools, ctx.agentId ?? "", ctx.agentId ? "" : (ctx.authSubject ?? "")],
    queryFn: () =>
      http.get<ToolListResponse>(
        "/v1/tools",
        ctx.agentId ? { agent_id: ctx.agentId } : { auth_subject: ctx.authSubject },
      ),
    staleTime: 60_000,
  });
}
