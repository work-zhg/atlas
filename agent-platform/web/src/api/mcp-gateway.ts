"use client";

/**
 * MCP 网关（atlas-mcpgateway，doc/mcp-design.html 第二版 §9）的管理端接口。
 *
 *   服务       注册、修改、停用 / 启用、立即刷新           MCP 管理员
 *   复核       按「工具 + 定义指纹」批准 / 拒绝             MCP 管理员
 *   API Key    发放、重置、停用、启用（只显示掩码）         MCP 管理员
 *   授权       给人、部门、数字员工授「使用」               该服务的所有者（用户中心校验）
 *   访问日志                                           MCP 管理员；所有者看自己的服务
 *
 * ★ 浏览器经网关同源调它（lib/http.ts 的 MCP_GATEWAY_API_BASE），身份由网关写入。
 * ★ 「被哪些智能体引用」只有 Atlas 知道：仍然走 Atlas 的 /v1/mcp/servers/{名字}（api/catalog.ts）。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { mcpGatewayHttp as gw } from "@/lib/http";

// ═══════════════════════════════════════════════ 类型（网关 api/schemas.py）

/** ok = 能选、能调；invalid = 机械检查不通过；pending_review；rejected */
export type GwToolStatus = "ok" | "invalid" | "pending_review" | "rejected";

export interface GwToolDef {
  description: string;
  input_schema: Record<string, unknown>;
  annotations: Record<string, unknown> | null;
  digest: string;
}

export interface GwTool extends GwToolDef {
  name: string;
  model_name: string | null;
  read_only: boolean;
  issues: string[];
  status: GwToolStatus;
  changed_at: string | null;
  /** 变化前的一版 */
  previous: GwToolDef | null;
  review: { decision: string; comment: string; reviewed_by: string; reviewed_at: string } | null;
}

export interface GwService {
  name: string;
  display_name: string;
  description: string;
  upstream_kind: string;
  upstream_name: string;
  credential_mode: string;
  review_required: boolean;
  call_timeout_s: number | null;
  enabled: boolean;
  created_by: string;
  updated_by: string;
  created_at: string;
  updated_at: string;
  fetched_at: string | null;
  fetch_error: string | null;
  changed_at: string | null;
  content_hash: string | null;
  /** 接入检查：不带网关凭据访问上游必须被拒（ok=true） */
  access_check: { ok: boolean | null; detail: string | null; checked_at: string | null };
  tool_counts: Partial<Record<GwToolStatus, number>>;
}

export interface GwServiceDetail extends GwService {
  tools: GwTool[];
  removed_tools: string[];
}

export interface GwServiceIn {
  name: string;
  display_name?: string;
  description?: string;
  upstream_name?: string | null;
  review_required: boolean;
  call_timeout_s?: number | null;
}

export type GwServicePatch = Partial<
  Pick<GwService, "display_name" | "description" | "upstream_name" | "review_required" | "call_timeout_s">
>;

export interface GwMe {
  uid: string;
  can_manage: boolean;
  /** 自己是所有者的服务 */
  owned_services: string[];
  owns_all: boolean;
}

/** 用户中心的授权行（开放接口原样返回） */
export interface GwAcl {
  id: string;
  permission: "READ" | "WRITE" | "OWNER";
  subject: {
    type: "USER" | "DEPT";
    id: string;
    name: string;
    account?: string;
    status?: string;
    user_type?: "human" | "digital";
  };
  include_sub: boolean;
  source?: string;
  granted_at: string;
}

export interface GwKey {
  id: string;
  subject_uid: string;
  subject_type: "human" | "digital";
  subject_name: string;
  /** 掩码：原文谁都看不到（Q3），数字员工的 Key 永远不显示 */
  masked: string;
  status: "active" | "disabled";
  issued_by: string;
  created_at: string;
  rotated_at: string;
  disabled_at: string | null;
}

export interface GwMissingKey {
  subject_uid: string;
  name: string;
  services: string[];
}

export interface GwAccessLog {
  id: number;
  at: string;
  service: string;
  subject_uid: string | null;
  user_uid: string | null;
  caller_app: string | null;
  run_id: string | null;
  http_method: string;
  rpc_method: string | null;
  tool: string | null;
  /** ok = 已转发；其余是拒绝原因 */
  outcome: string;
  status: number;
  forwarded: boolean;
  duration_ms: number;
}

export const GW_OUTCOME: Record<string, string> = {
  ok: "成功",
  service_not_found: "服务不存在",
  service_disabled: "服务已停用",
  key_missing: "没带 Key",
  key_invalid: "Key 无效",
  forbidden: "无权",
  authz_unavailable: "判权不可用",
  tool_unknown: "工具不存在",
  tool_pending_review: "待复核",
  tool_rejected: "复核拒绝",
  tool_invalid: "定义不可用",
  upstream_unavailable: "上游不可用",
  upstream_rejected: "上游拒绝网关凭据",
};

// ═══════════════════════════════════════════════ 查询

const k = {
  me: ["mcpgw", "me"] as const,
  services: ["mcpgw", "services"] as const,
  service: (name: string) => ["mcpgw", "service", name] as const,
  reviews: ["mcpgw", "reviews"] as const,
  grants: (name: string) => ["mcpgw", "grants", name] as const,
  holders: (name: string) => ["mcpgw", "holders", name] as const,
  keys: ["mcpgw", "keys"] as const,
  missing: ["mcpgw", "keys", "missing"] as const,
  log: (q: Record<string, string | undefined>) => ["mcpgw", "log", q] as const,
};

export function useGwMe() {
  return useQuery({
    queryKey: k.me,
    queryFn: () => gw.get<GwMe>("/api/v1/me"),
    staleTime: 60_000,
    // 网关没部署 / 连不上：导航里不显示 MCP，不反复重试
    retry: false,
  });
}

export function useGwServices() {
  return useQuery({
    queryKey: k.services,
    queryFn: () => gw.get<GwService[]>("/api/v1/services"),
    staleTime: 15_000,
    retry: 1,
  });
}

export function useGwService(name: string | undefined) {
  return useQuery({
    queryKey: k.service(name ?? ""),
    queryFn: () => gw.get<GwServiceDetail>(`/api/v1/services/${name}`),
    enabled: !!name,
  });
}

export function useGwPendingReviews(enabled: boolean) {
  return useQuery({
    queryKey: k.reviews,
    queryFn: () => gw.get<{ service: string; tool: GwTool }[]>("/api/v1/reviews"),
    enabled,
  });
}

export function useGwGrants(name: string | undefined) {
  return useQuery({
    queryKey: k.grants(name ?? ""),
    queryFn: () => gw.get<GwAcl[]>(`/api/v1/services/${name}/grants`),
    enabled: !!name,
  });
}

export function useGwKeys(enabled: boolean) {
  return useQuery({ queryKey: k.keys, queryFn: () => gw.get<GwKey[]>("/api/v1/keys"), enabled });
}

export function useGwMissingKeys(enabled: boolean) {
  return useQuery({
    queryKey: k.missing,
    queryFn: () => gw.get<GwMissingKey[]>("/api/v1/keys/missing"),
    enabled,
  });
}

export function useGwAccessLog(q: { service?: string; outcome?: string }, enabled: boolean) {
  return useQuery({
    queryKey: k.log(q),
    queryFn: () => gw.get<GwAccessLog[]>("/api/v1/access-log", { ...q, limit: 200 }),
    enabled,
  });
}

// ═══════════════════════════════════════════════ 写

function useDone() {
  const qc = useQueryClient();
  return (name?: string) => {
    void qc.invalidateQueries({ queryKey: k.services });
    void qc.invalidateQueries({ queryKey: k.reviews });
    if (name) void qc.invalidateQueries({ queryKey: k.service(name) });
    // 编辑器的工具目录来自 Atlas（读网关的元数据，有缓存）
    void qc.invalidateQueries({ queryKey: ["tools"] });
  };
}

export function useCreateGwService() {
  const done = useDone();
  return useMutation({
    mutationFn: (body: GwServiceIn) => gw.post<GwServiceDetail>("/api/v1/services", body),
    onSuccess: (d) => done(d.name),
  });
}

export function usePatchGwService(name: string) {
  const done = useDone();
  return useMutation({
    mutationFn: (patch: GwServicePatch) => gw.patch<GwServiceDetail>(`/api/v1/services/${name}`, patch),
    onSuccess: () => done(name),
  });
}

export function useSetGwServiceEnabled(name: string) {
  const done = useDone();
  return useMutation({
    mutationFn: (enabled: boolean) =>
      gw.post<GwService>(`/api/v1/services/${name}/${enabled ? "enable" : "disable"}`),
    onSuccess: () => done(name),
  });
}

export function useRefreshGwService(name: string) {
  const done = useDone();
  return useMutation({
    mutationFn: () => gw.post<GwServiceDetail>(`/api/v1/services/${name}/refresh`),
    onSuccess: () => done(name),
  });
}

export function useReviewGwTool() {
  const done = useDone();
  return useMutation({
    mutationFn: (input: {
      service: string;
      tool: string;
      digest: string;
      decision: "approved" | "rejected";
      comment?: string;
    }) =>
      gw.post<GwTool>(`/api/v1/services/${input.service}/tools/${input.tool}/review`, {
        digest: input.digest,
        decision: input.decision,
        comment: input.comment ?? "",
      }),
    onSuccess: (_d, input) => done(input.service),
  });
}

export function useGwGrantActions(name: string) {
  const qc = useQueryClient();
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: k.grants(name) });
    void qc.invalidateQueries({ queryKey: k.missing });
  };
  const add = useMutation({
    mutationFn: (body: {
      level: "READ" | "OWNER";
      subjects: { type: "user" | "dept"; id: string }[];
      include_sub: boolean;
    }) => gw.post(`/api/v1/services/${name}/grants`, body),
    onSuccess: refresh,
  });
  const revoke = useMutation({
    mutationFn: (aclId: string) => gw.del(`/api/v1/services/${name}/grants/${aclId}`),
    onSuccess: refresh,
  });
  return { add, revoke };
}

export function useGwKeyActions() {
  const qc = useQueryClient();
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: k.keys });
    void qc.invalidateQueries({ queryKey: k.missing });
  };
  const issue = useMutation({
    mutationFn: (body: { subject_uid: string; services: string[] }) => gw.post<GwKey>("/api/v1/keys", body),
    onSuccess: refresh,
  });
  const act = useMutation({
    mutationFn: (input: { id: string; action: "reset" | "disable" | "enable" }) =>
      gw.post<GwKey>(`/api/v1/keys/${input.id}/${input.action}`),
    onSuccess: refresh,
  });
  return { issue, act };
}
