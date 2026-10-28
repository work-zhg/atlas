"use client";

import { useQuery } from "@tanstack/react-query";

import { http } from "@/lib/http";
import { qk } from "./keys";
import type { ModelInfo, ToolInfo } from "./types";

/**
 * 编辑器里的模型从哪来（经模型管理平台时才有，agent-platform/doc/design/model-design.html 7.2）。
 *
 *   none            新建的智能体（还没保存）：列出平台上登记的全部模型
 *   pending_models  它的客户端 Key 还没分配模型：同上，分配之前不能运行
 *   active          列出 Key 的可用模型
 *   blocked         Key 被停用；no_models 分配的模型都不可用了
 */
export interface ModelKeyInfo {
  status: "none" | "pending_models" | "active" | "blocked" | "no_models";
  notes: string[];
}

interface ModelListResponse {
  data: ModelInfo[];
  gateway_reachable: boolean;
  key?: ModelKeyInfo | null;
}

/**
 * 模型目录 —— 编辑器据此决定渲染 effort 分段器还是 temperature 滑块（§3 D3）。
 * 每个模型收不收哪些参数由目录给出，前端不要自己硬编码模型名判断。
 * 经模型管理平台时按这个智能体的客户端 Key 列出（与权限类型无关）。
 */
export function useModels(ctx: { agentId?: string } = {}) {
  return useQuery({
    queryKey: [...qk.models, ctx.agentId ?? ""],
    queryFn: () =>
      http.get<ModelListResponse>("/v1/models", ctx.agentId ? { agent_id: ctx.agentId } : undefined),
    // 管理员分配模型后要能看到：和工具列表一样缓 1 分钟
    staleTime: 60_000,
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

/** 编辑器里可选的知识库按谁的权限列出（同 MCP：运行时知识库按同一个主体判） */
export interface KnowledgeSubjectInfo {
  /** user = 以人为权限（当前用户）；agent = 以 Agent 为权限（它的数字员工） */
  kind: "user" | "agent";
  /** 以 Agent 为权限、还没有数字员工（没保存过）时为空 */
  uid: string | null;
  notes: string[];
}

export interface KnowledgeBaseInfo {
  id: string;
  name: string;
  description?: string | null;
  icon?: string | null;
}

export interface KnowledgeListResponse {
  data: KnowledgeBaseInfo[];
  subject: KnowledgeSubjectInfo;
}

/**
 * 可绑定的知识库：权限主体能读的。编辑已有的给 agentId，新建的给 authSubject（同 useTools）。
 * ★ 绑定不是授权：以 Agent 为权限时要先把库共享给它的数字员工。
 */
export function useKnowledgeBases(ctx: { agentId?: string; authSubject?: "user" | "agent" } = {}) {
  return useQuery({
    queryKey: [...qk.knowledgeBases, ctx.agentId ?? "", ctx.agentId ? "" : (ctx.authSubject ?? "")],
    queryFn: () =>
      http.get<KnowledgeListResponse>(
        "/v1/knowledge-bases",
        ctx.agentId ? { agent_id: ctx.agentId } : { auth_subject: ctx.authSubject },
      ),
    staleTime: 60_000,
  });
}
