"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { http } from "@/lib/http";
import { qk } from "./keys";
import type {
  Agent,
  AgentCreate,
  AgentDetail,
  AgentIdentity,
  AgentStatus,
  AgentUpdate,
  AgentVersion,
  AuthSubject,
  Page,
} from "./types";

export interface AgentListParams {
  q?: string;
  status?: AgentStatus | "all";
  model?: string;
  limit?: number;
}

/** 注意：/v1/agents 只有 limit（≤200），**没有游标** —— 智能体数量级不需要分页。 */
export function useAgents(params: AgentListParams = {}) {
  const { q, status, model, limit = 100 } = params;
  return useQuery({
    queryKey: [...qk.agents.list(status), { q, model, limit }],
    queryFn: () =>
      http.get<Page<Agent>>("/v1/agents", {
        q,
        status: status === "all" ? undefined : status,
        model,
        limit,
      }),
  });
}

export function useAgent(agentId: string | undefined) {
  return useQuery({
    queryKey: qk.agents.detail(agentId ?? ""),
    queryFn: () => http.get<AgentDetail>(`/v1/agents/${agentId}`),
    enabled: Boolean(agentId),
  });
}

export function useAgentVersions(agentId: string | undefined) {
  return useQuery({
    queryKey: qk.agents.versions(agentId ?? ""),
    queryFn: () => http.get<Page<AgentVersion>>(`/v1/agents/${agentId}/versions`),
    enabled: Boolean(agentId),
  });
}

export function useCreateAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: AgentCreate) => http.post<AgentDetail>("/v1/agents", body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.agents.all }),
  });
}

/**
 * 保存 = 新建版本，不是就地覆盖（§5.4）。
 * 历史 run 仍指向旧版本快照，所以详情和版本列表都要失效。
 */
export function useUpdateAgent(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: AgentUpdate) => http.patch<AgentDetail>(`/v1/agents/${agentId}`, body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.agents.all }),
  });
}

/** 归档/启用。★ 是软删除语义 —— 物理删除会毁掉历史 run 的可复现性。 */
export function useSetAgentStatus(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (status: AgentStatus) =>
      http.post<AgentDetail>(`/v1/agents/${agentId}/status`, { status }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.agents.all }),
  });
}

/** 切换权限类型（只有所有者）：只影响之后新建的会话轮次，进行中的不变。 */
export function useSetAuthSubject(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (auth_subject: AuthSubject) =>
      http.put<AgentDetail>(`/v1/agents/${agentId}/auth-subject`, { auth_subject }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.agents.all }),
  });
}

/** 「身份」：数字员工、权限类型、所配知识库它能不能读（数字员工设计 §6.1）。 */
export function useAgentIdentity(agentId: string | undefined) {
  return useQuery({
    queryKey: ["agents", "identity", agentId ?? ""],
    queryFn: () => http.get<AgentIdentity>(`/v1/agents/${agentId}/identity`),
    enabled: Boolean(agentId),
  });
}

// ---------- 共享（用户中心数据权限 AtlasAgent，数字员工设计 §8.1）

export type AgentLevel = "READ" | "WRITE" | "OWNER";

export interface AgentGrant {
  id: string;
  permission: AgentLevel;
  subject: {
    type: "USER" | "DEPT";
    id: string;
    name?: string;
    account?: string;
    status?: string;
    user_type?: "human" | "digital";
  };
  include_sub: boolean;
}

export interface AgentGrants {
  data: AgentGrant[];
  my_level: AgentLevel;
}

export function useAgentGrants(agentId: string | undefined) {
  return useQuery({
    queryKey: ["agents", "grants", agentId ?? ""],
    queryFn: () => http.get<AgentGrants>(`/v1/agents/${agentId}/grants`),
    enabled: Boolean(agentId),
  });
}

export const agentGrantsApi = {
  add: (
    agentId: string,
    body: { level: AgentLevel; subjects: { type: "user" | "dept"; id: string }[]; include_sub: boolean },
  ) => http.post(`/v1/agents/${agentId}/grants`, body),
  update: (agentId: string, aclId: string, body: { level?: AgentLevel; include_sub?: boolean }) =>
    http.patch(`/v1/agents/${agentId}/grants/${aclId}`, body),
  revoke: (agentId: string, aclId: string) => http.del(`/v1/agents/${agentId}/grants/${aclId}`),
};
