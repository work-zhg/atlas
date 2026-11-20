"use client";

/**
 * 模型平台（atlas-modelgateway，agent-platform/doc/design/model-gateway-design.html）的管理端接口。
 *
 *   账号         公司在各供应商的账号：地址、密钥、出网方式；状态、余额            管理员
 *   模型         对外的模型名 + 线路（哪个账号、上游叫什么、优先级、权重）、能力、价格  管理员
 *   授权         私有模型授权给用户、部门（可含下级）                              管理员
 *   我的 Key     一人可以多把：用途名、模型、月预算、每分钟次数、到期              所有人
 *   Key 管理     全部个人 Key 和客户端 Key：分配模型、限额、停用                   管理员
 *   调用记录     每次调用一条：走了哪些线路、用量、花费                            个人看自己的，管理员看全部
 *
 * ★ 调用经模型网关（/llm），不经这里；浏览器经网关同源调管理端（lib/http.ts 的 MODEL_GATEWAY_API_BASE）。
 * ★ 金额是后端的 Decimal，JSON 里是字符串：显示时用 money() 转。
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { modelGatewayHttp as gw } from "@/lib/http";

// ═══════════════════════════════════════════════ 类型（平台 api/schemas.py）

export type MgVisibility = "public" | "private";
export type MgKind = "chat" | "embedding" | "rerank";
/** 线路 / 账号状态：正常、冷却中（限流、过载后歇一会）、异常（密钥失效、欠费）、已停用 */
export type MgHealth = "ok" | "cooling" | "abnormal" | "disabled";

export interface MgMe {
  uid: string;
  can_manage: boolean;
  /** 模型网关对外的地址（Anthropic 格式的 base_url；OpenAI 格式加 /v1）；空 = 还没对外开放 */
  gateway_url: string | null;
}

export interface MgProvider {
  code: string;
  name: string;
  /** 提供哪些格式（anthropic / openai / google），第一个是推荐的 */
  formats: string[];
  /** 各格式的默认地址；空串 = 必须手填 */
  endpoints: Record<string, string>;
  can_balance: boolean;
  can_list_models: boolean;
  key_optional: boolean;
  needs_endpoint: boolean;
  /** 境外供应商：一般要经出网代理 */
  overseas: boolean;
  hint: string;
}

export interface MgAccount {
  id: string;
  name: string;
  provider: string;
  provider_name: string;
  endpoints: Record<string, string>;
  secret_masked: string | null;
  egress: "direct" | "proxy";
  first_content_timeout_s: number | null;
  balance_alert: string | null;
  balance: string | null;
  balance_at: string | null;
  remark: string;
  enabled: boolean;
  status: MgHealth;
  issue: { kind: string; since: string; message: string } | null;
  cooling_routes: number;
  low_balance: boolean;
  routes: number;
  checked_at: string | null;
  check_error: string;
  created_by: string;
  created_at: string;
}

export interface MgAccountIn {
  name: string;
  provider: string;
  endpoints: Record<string, string>;
  /** 新建必填（自部署可空）；修改时不传 = 不改 */
  secret?: string | null;
  egress: "direct" | "proxy";
  first_content_timeout_s?: number | null;
  balance_alert?: number | null;
  remark: string;
  enabled?: boolean;
}

export interface MgProbe {
  ok: boolean;
  steps: { name: string; ok: boolean; message: string }[];
  upstream_models: string[] | null;
  balance: string | null;
}

export interface MgCaps {
  vision: boolean;
  tools: boolean;
  thinking: "none" | "optional" | "always_on";
  context_window: number | null;
  max_output: number | null;
}

/** 元 / 百万 token */
export interface MgPrice {
  input: string | number;
  cache_hit: string | number;
  output: string | number;
}

export interface MgRoute {
  id: string;
  account_id: string;
  account_name: string;
  provider: string;
  upstream_model: string;
  priority: number;
  weight: number;
  status: MgHealth;
}

export interface MgRouteIn {
  account_id: string;
  /** 空 = 和模型名一样 */
  upstream_model: string;
  priority: number;
  weight: number;
}

export interface MgGrant {
  id: string;
  subject_type: "user" | "dept" | string;
  subject_id: string;
  subject_name: string;
  permission: string;
  include_sub: boolean;
}

export interface MgGrantIn {
  subject_type: "user" | "dept";
  subject_id: string;
  include_sub: boolean;
}

export interface MgModel {
  id: string;
  name: string;
  display_name: string;
  kind: MgKind;
  visibility: MgVisibility;
  description: string;
  caps: MgCaps;
  price: MgPrice;
  enabled: boolean;
  /** available；degraded = 首选线路不可用，走备用；unavailable；disabled */
  status: "available" | "degraded" | "unavailable" | "disabled";
  /** 管理员才有 */
  routes: MgRoute[];
  /** 普通用户看：public = 公有；granted = 授权给了你或你所在的部门 */
  access: "public" | "granted" | null;
  /** 管理员看：私有模型的授权 */
  grants: MgGrant[] | null;
  /** 多少把 Key 选了它 */
  keys: number;
}

export interface MgModelUpdate {
  display_name: string;
  visibility: MgVisibility;
  description: string;
  /** 不传 = 新建时按第一条线路的预置带出 / 修改时不改 */
  caps?: MgCaps | null;
  price: { input: number; cache_hit: number; output: number };
  enabled: boolean;
  routes: MgRouteIn[];
}

export interface MgModelIn extends MgModelUpdate {
  name: string;
  kind: MgKind;
  grants: MgGrantIn[];
}

export type MgKeyStatus = "pending" | "active" | "blocked";

export interface MgKeyModel {
  name: string;
  display_name: string;
  /** ok；disabled = 模型停用；revoked = 授权已收回；missing = 模型已删除 */
  state: "ok" | "disabled" | "revoked" | "missing";
}

export interface MgKey {
  id: string;
  key_type: "user" | "client";
  label: string;
  /** 个人 Key 是持有人，客户端 Key 是对象名称 */
  name: string;
  user_id: string | null;
  client_type: string | null;
  client_code: string | null;
  models: MgKeyModel[];
  status: MgKeyStatus;
  block_reason: string;
  masked: string | null;
  monthly_budget: string | null;
  rpm: number | null;
  expires_at: string | null;
  spent_this_month: string;
  last_used_at: string | null;
  created_at: string;
  rotated_at: string | null;
}

export interface MgKeyWithSecret extends MgKey {
  /** 原文：只在新建、重置时返回一次 */
  secret: string | null;
}

export interface MgKeyIn {
  label: string;
  models: string[];
  monthly_budget: number | null;
  rpm: number | null;
  expires_at: string | null;
}

export interface MgClientType {
  code: string;
  apps: string[];
  default_public: boolean;
}

export interface MgAttempt {
  route: string;
  account: string;
  provider: string;
  upstream_model: string;
  path: string;
  outcome: string;
  message: string;
  ms: number;
  first_content_ms: number | null;
}

export interface MgCall {
  id: string;
  started_at: string;
  model: string;
  key_id: string | null;
  key_kind: string;
  session: string | null;
  entry_format: string;
  status: "ok" | "error" | "interrupted";
  error_category: string | null;
  attempts: MgAttempt[];
  first_content_ms: number | null;
  duration_ms: number;
  input_tokens: number;
  cache_write_tokens: number;
  cache_hit_tokens: number;
  output_tokens: number;
  estimated: boolean;
  cost: string;
  notes: Record<string, unknown>;
}

export interface MgCallsPage {
  data: MgCall[];
  next_before: string | null;
}

export interface MgCallSummary {
  group: string;
  label: string;
  calls: number;
  errors: number;
  input: number;
  cache_write: number;
  cache_hit: number;
  output: number;
  cost: string;
}

export interface MgCallFilter {
  scope: "mine" | "all";
  model?: string;
  status?: string;
  key_id?: string;
  since?: string;
  until?: string;
}

export const MG_HEALTH: Record<MgHealth, { label: string; color: string }> = {
  ok: { label: "正常", color: "success" },
  cooling: { label: "冷却中", color: "warning" },
  abnormal: { label: "异常", color: "error" },
  disabled: { label: "已停用", color: "default" },
};

export const MG_MODEL_STATUS: Record<MgModel["status"], { label: string; color: string; hint: string }> = {
  available: { label: "可用", color: "success", hint: "" },
  degraded: { label: "走备用线路", color: "warning", hint: "首选线路暂时不可用，调用正常，走的是其他线路" },
  unavailable: { label: "不可用", color: "error", hint: "所有线路都不可用，调用会报错" },
  disabled: { label: "已停用", color: "default", hint: "调用会返回「模型已停用」" },
};

export const MG_KEY_STATUS: Record<MgKeyStatus, { label: string; color: string }> = {
  pending: { label: "待分配模型", color: "warning" },
  active: { label: "正常", color: "success" },
  blocked: { label: "已停用", color: "default" },
};

export const MG_KEY_MODEL_STATE: Record<MgKeyModel["state"], string> = {
  ok: "",
  disabled: "模型已停用",
  revoked: "授权已收回",
  missing: "模型已删除",
};

/** 客户端类型的中文名（平台只给编码） */
export const MG_CLIENT_TYPE: Record<string, string> = { Agent: "智能体", Service: "平台服务" };

// ═══════════════════════════════════════════════ 查询

const k = {
  me: ["modelgw", "me"] as const,
  providers: ["modelgw", "providers"] as const,
  accounts: ["modelgw", "accounts"] as const,
  models: ["modelgw", "models"] as const,
  myKeys: ["modelgw", "keys", "me"] as const,
  keys: (q: Record<string, string | undefined>) => ["modelgw", "keys", "all", q] as const,
  clientTypes: ["modelgw", "client-types"] as const,
  calls: (f: MgCallFilter) => ["modelgw", "calls", f] as const,
  summary: (by: string, f: MgCallFilter) => ["modelgw", "calls", "summary", by, f] as const,
};

export function useMgMe() {
  return useQuery({
    queryKey: k.me,
    queryFn: () => gw.get<MgMe>("/api/v1/me"),
    staleTime: 60_000,
    // 平台没部署 / 连不上：导航里不显示「模型」，不反复重试
    retry: false,
  });
}

export function useMgProviders(enabled = true) {
  return useQuery({
    queryKey: k.providers,
    queryFn: () => gw.get<MgProvider[]>("/api/v1/providers"),
    staleTime: 10 * 60_000,
    enabled,
  });
}

export function useMgAccounts(enabled = true) {
  return useQuery({
    queryKey: k.accounts,
    queryFn: () => gw.get<MgAccount[]>("/api/v1/accounts"),
    staleTime: 10_000,
    enabled,
  });
}

export function useMgModels() {
  return useQuery({
    queryKey: k.models,
    queryFn: () => gw.get<MgModel[]>("/api/v1/models"),
    staleTime: 10_000,
    retry: 1,
  });
}

export function useMgMyKeys() {
  return useQuery({ queryKey: k.myKeys, queryFn: () => gw.get<MgKey[]>("/api/v1/keys/me") });
}

export function useMgKeys(q: { key_type?: string; client_type?: string; status?: string; q?: string }, enabled = true) {
  return useQuery({
    queryKey: k.keys(q),
    queryFn: () => gw.get<MgKey[]>("/api/v1/keys", q),
    enabled,
  });
}

export function useMgClientTypes(enabled = true) {
  return useQuery({
    queryKey: k.clientTypes,
    queryFn: () => gw.get<MgClientType[]>("/api/v1/client-types"),
    staleTime: 10 * 60_000,
    enabled,
  });
}

export function useMgCalls(f: MgCallFilter) {
  return useInfiniteQuery({
    queryKey: k.calls(f),
    queryFn: ({ pageParam }) =>
      gw.get<MgCallsPage>("/api/v1/calls", { ...f, before: pageParam, limit: 50 }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_before,
  });
}

export function useMgCallSummary(by: "model" | "key" | "day", f: MgCallFilter) {
  return useQuery({
    queryKey: k.summary(by, f),
    queryFn: () =>
      gw.get<MgCallSummary[]>("/api/v1/calls/summary", { by, scope: f.scope, since: f.since, until: f.until }),
  });
}

// ═══════════════════════════════════════════════ 写

/** 模型、账号、Key 变了：平台的各列表、Atlas 编辑器的模型列表都要刷新 */
function useRefresh() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: ["modelgw"] });
    void qc.invalidateQueries({ queryKey: ["models"] });
  };
}

export function useMgAccountActions() {
  const refresh = useRefresh();
  const save = useMutation({
    mutationFn: (input: { id?: string; body: MgAccountIn }) =>
      input.id
        ? gw.put<MgAccount>(`/api/v1/accounts/${input.id}`, input.body)
        : gw.post<MgAccount>("/api/v1/accounts", input.body),
    onSuccess: refresh,
  });
  const toggle = useMutation({
    mutationFn: (input: { id: string; enabled: boolean }) =>
      gw.post<MgAccount>(`/api/v1/accounts/${input.id}/${input.enabled ? "enable" : "disable"}`),
    onSuccess: refresh,
  });
  const remove = useMutation({ mutationFn: (id: string) => gw.del(`/api/v1/accounts/${id}`), onSuccess: refresh });
  /** 已保存的账号：通过了顺带解除异常 */
  const test = useMutation({
    mutationFn: (input: { id: string; upstream_model?: string }) =>
      gw.post<MgProbe>(`/api/v1/accounts/${input.id}/test`, { upstream_model: input.upstream_model || null }),
    onSuccess: refresh,
  });
  /** 保存前试一下 */
  const testDraft = useMutation({
    mutationFn: (body: MgAccountIn & { upstream_model?: string }) => gw.post<MgProbe>("/api/v1/accounts/test", body),
  });
  return { save, toggle, remove, test, testDraft };
}

export function useMgModelActions() {
  const refresh = useRefresh();
  const save = useMutation({
    mutationFn: (input: { kind: "new"; body: MgModelIn } | { kind: "edit"; id: string; body: MgModelUpdate }) =>
      input.kind === "new"
        ? gw.post<MgModel>("/api/v1/models", input.body)
        : gw.put<MgModel>(`/api/v1/models/${input.id}`, input.body),
    onSuccess: refresh,
  });
  const toggle = useMutation({
    mutationFn: (input: { id: string; enabled: boolean }) =>
      gw.post<MgModel>(`/api/v1/models/${input.id}/${input.enabled ? "enable" : "disable"}`),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (id: string) => gw.del<{ deleted: string; keys_updated: number }>(`/api/v1/models/${id}`),
    onSuccess: refresh,
  });
  return { save, toggle, remove };
}

/** 选好账号和上游模型后，按预置带出能力（没有预置的给保守值） */
export function suggestCaps(accountId: string, upstreamModel: string, kind: MgKind) {
  return gw.get<{ caps: MgCaps; preset: boolean }>("/api/v1/models/caps-suggest", {
    account_id: accountId,
    upstream_model: upstreamModel,
    kind,
  });
}

export function useMgGrantActions(id: string) {
  const refresh = useRefresh();
  const add = useMutation({
    mutationFn: (body: MgGrantIn[]) => gw.post<MgGrant[]>(`/api/v1/models/${id}/grants`, body),
    onSuccess: refresh,
  });
  const revoke = useMutation({
    mutationFn: (aclId: string) => gw.del(`/api/v1/models/${id}/grants/${aclId}`),
    onSuccess: refresh,
  });
  return { add, revoke };
}

/** 我的 Key */
export function useMgMyKeyActions() {
  const refresh = useRefresh();
  const create = useMutation({
    mutationFn: (body: MgKeyIn) => gw.post<MgKeyWithSecret>("/api/v1/keys/me", body),
    onSuccess: refresh,
  });
  const update = useMutation({
    mutationFn: (input: { id: string; body: Partial<MgKeyIn> }) => gw.put<MgKey>(`/api/v1/keys/me/${input.id}`, input.body),
    onSuccess: refresh,
  });
  const act = useMutation({
    mutationFn: (input: { id: string; action: "reset" | "block" | "unblock" }) =>
      gw.post<MgKeyWithSecret>(`/api/v1/keys/me/${input.id}/${input.action}`),
    onSuccess: refresh,
  });
  const remove = useMutation({ mutationFn: (id: string) => gw.del(`/api/v1/keys/me/${id}`), onSuccess: refresh });
  return { create, update, act, remove };
}

/** Key 管理（管理员）：个人 Key 只能改限额、停用、删除；客户端 Key 还能分配模型、重置 */
export function useMgKeyAdminActions() {
  const refresh = useRefresh();
  const createClient = useMutation({
    mutationFn: (body: MgKeyIn & { client_type: string; client_code: string; name: string }) =>
      gw.post<MgKey>("/api/v1/client-keys", body),
    onSuccess: refresh,
  });
  const update = useMutation({
    mutationFn: (input: { id: string; body: Partial<MgKeyIn> }) => gw.put<MgKey>(`/api/v1/keys/${input.id}`, input.body),
    onSuccess: refresh,
  });
  const reset = useMutation({ mutationFn: (id: string) => gw.post<MgKey>(`/api/v1/keys/${id}/reset`), onSuccess: refresh });
  const block = useMutation({
    mutationFn: (input: { id: string; reason: string }) => gw.post<MgKey>(`/api/v1/keys/${input.id}/block`, { reason: input.reason }),
    onSuccess: refresh,
  });
  const unblock = useMutation({ mutationFn: (id: string) => gw.post<MgKey>(`/api/v1/keys/${id}/unblock`), onSuccess: refresh });
  const remove = useMutation({ mutationFn: (id: string) => gw.del(`/api/v1/keys/${id}`), onSuccess: refresh });
  return { createClient, update, reset, block, unblock, remove };
}
