"use client";

/**
 * MCP 网关（atlas-mcpgateway，原型 agent-platform/doc/prototype/mcp-gateway.html）的管理端接口。
 *
 *   服务       登记（MCP 代理 / REST 转换 / gRPC 转换）、改连接和凭据、停用；工具的增删改、停用、试调   MCP 管理员
 *   授权       给 API Key 授服务级或工具级权限；增删所有者                                      服务所有者、MCP 管理员
 *   API Key    用户 Key（本人申请）；智能体 Key（Atlas 建智能体时申请，管理员可补申请）            本人、MCP 管理员
 *
 * ★ 浏览器经网关同源调它（lib/http.ts 的 MCP_GATEWAY_API_BASE），身份由网关写入。
 * ★ 「被哪些智能体引用」只有 Atlas 知道：仍然走 Atlas 的 /v1/mcp/servers/{名字}（api/catalog.ts）。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { mcpGatewayHttp as gw } from "@/lib/http";

// ═══════════════════════════════════════════════ 类型（网关 api/schemas.py）

export type GwKind = "mcp" | "rest" | "grpc";
export type GwVisibility = "public" | "private";
export type GwEffect = "read" | "write" | "destructive";

export const GW_KIND: Record<GwKind, { label: string; desc: string }> = {
  mcp: { label: "MCP 代理", desc: "连接远程 MCP Server（Streamable HTTP 或旧版 SSE），工具定义从上游拉取" },
  rest: { label: "REST 转换", desc: "导入 OpenAPI 或手工定义，把 HTTP 接口转换成 MCP 工具" },
  grpc: { label: "gRPC 转换", desc: "服务端反射或上传描述文件，把 gRPC 方法转换成 MCP 工具" },
};

export const GW_EFFECT: Record<GwEffect, { label: string; color: string }> = {
  read: { label: "只读", color: "success" },
  write: { label: "有副作用", color: "warning" },
  destructive: { label: "破坏性", color: "error" },
};

/** 每种服务能用哪些上游凭据（网关 upstreams/credentials.py 的 BY_KIND） */
export const GW_CREDENTIAL_TYPES: Record<GwKind, string[]> = {
  mcp: ["none", "bearer", "header", "oauth2"],
  rest: ["none", "apikey", "bearer", "basic", "oauth2"],
  grpc: ["none", "metadata", "mtls"],
};

export const GW_CREDENTIAL_LABEL: Record<string, string> = {
  none: "不需要",
  apikey: "API Key",
  bearer: "Bearer Token",
  header: "自定义请求头",
  basic: "Basic（用户名 + 密码）",
  oauth2: "OAuth2 client_credentials",
  metadata: "metadata 键值",
  mtls: "mTLS 客户端证书",
};

export interface GwCredential {
  type: string;
  key?: string | null;
  where?: string | null;
  user?: string | null;
  token_url?: string | null;
  scope?: string | null;
  cert?: string | null;
  /** 「••••末 4 位」；没有为 null */
  hint?: string | null;
}

/** secret 留空 = 沿用原来的（类型没变时） */
export interface GwCredentialIn extends Omit<GwCredential, "hint"> {
  secret?: string | null;
}

export interface GwMyAccess {
  owner: boolean;
  /** public = 公开服务；all = 服务级；tools = 工具级；none = 没有授权；no_key = 还没有用户 Key */
  scope: "public" | "all" | "tools" | "none" | "no_key";
  tools: string[];
}

export interface GwService {
  name: string;
  display_name: string;
  description: string;
  kind: GwKind;
  visibility: GwVisibility;
  enabled: boolean;
  /** mcp：url、transport；rest：base_url、spec_url；grpc：address、tls、discovery、services */
  connection: Record<string, unknown>;
  credential: GwCredential;
  pass_identity: boolean;
  created_by: string;
  created_at: string;
  updated_at: string;
  fetched_at: string | null;
  fetch_error: string | null;
  changed_at: string | null;
  tool_count: number;
  active_tool_count: number;
  my: GwMyAccess;
}

export interface GwRestParam {
  name: string;
  in: "path" | "query" | "header" | "body";
  type: string;
  required: boolean;
  /** 固定值：每次都按它发给上游，不出现在工具参数里 */
  fixed: string;
  description: string;
  /** 发给上游时用的名字（缺省同 name）；整个请求体是 $ */
  field: string;
}

export interface GwRestMapping {
  method: string;
  path: string;
  params: GwRestParam[];
  /** JSONPath，只取响应里的这部分；空 = 整个响应体 */
  pick: string;
  limit_kb: number;
}

export interface GwTool {
  name: string;
  /** 模型看到的名字（服务名__工具名）；不合规时为空 */
  model_name: string | null;
  description: string;
  input_schema: Record<string, unknown>;
  annotations: Record<string, unknown> | null;
  effect: GwEffect;
  /** REST：GwRestMapping；gRPC：method、stream、request、response；MCP 代理为 null */
  mapping: Record<string, unknown> | null;
  status: "active" | "disabled";
  issues: string[];
  digest: string;
  changed_at: string | null;
  previous: Record<string, unknown> | null;
  /** 能被调用：没停用、没删除、定义合规 */
  callable: boolean;
}

export interface GwServiceDetail extends GwService {
  tools: GwTool[];
  removed_tools: string[];
  /** 能改服务定义（MCP 管理员）/ 能授权（所有者、管理员） */
  can_manage: boolean;
  can_assign: boolean;
}

export interface GwDiscoveredTool {
  /** REST：「GET /path」；gRPC：「包.服务/方法」；MCP：工具名 */
  key: string;
  name: string;
  label: string;
  description: string;
  effect: GwEffect;
  input_schema: Record<string, unknown>;
  /** gRPC：unary | server | client | bidi */
  stream?: string | null;
  unsupported?: string | null;
  issues: string[];
  /** 重新导入时：服务里已经有同名的工具 */
  exists: boolean;
}

export interface GwDiscovered {
  kind: GwKind;
  tools: GwDiscoveredTool[];
  /** gRPC：描述里的服务 */
  services: string[];
}

export interface GwMe {
  uid: string;
  can_manage: boolean;
  owned_services: string[];
  owns_all: boolean;
  has_key: boolean;
  /** 外部客户端连网关的地址；null = 还没对集群外开放 */
  public_url: string | null;
}

export interface GwKeyRef {
  id: string;
  key_type: "user" | "client";
  name: string;
  status: "active" | "blocked";
  subject_uid: string;
  client_code: string | null;
}

export interface GwKeyGrant {
  key: GwKeyRef;
  /** all = 全部工具（含以后新增的）；tools = 只有 tools 里这几个 */
  scope: "all" | "tools";
  tools: string[];
}

export interface GwOwner {
  user_id: string;
  name: string;
}

export interface GwServerGrants {
  owners: GwOwner[];
  keys: GwKeyGrant[];
}

export interface GwServiceScope {
  service: string;
  display_name: string;
  scope: "all" | "tools";
  tools: string[];
}

export interface GwKey {
  id: string;
  key_type: "user" | "client";
  user_uid: string | null;
  client_type: string | null;
  client_code: string | null;
  name: string;
  /** 用户中心里的主体：授权授给它，网关判权时作 X-Auth-Uid */
  subject_uid: string;
  /** 掩码，原文不返回 */
  masked: string;
  status: "active" | "blocked";
  created_by: string;
  created_at: string;
  rotated_at: string;
  /** 私有服务上的权限（取不到时为空） */
  grants: GwServiceScope[];
}

export interface GwKeySecret extends GwKey {
  /** 原文：只在申请、重置用户 Key 时返回这一次 */
  secret: string;
}

export interface GwRpcError {
  code: number;
  message: string;
  data?: Record<string, unknown>;
}

export interface GwTryOut {
  result: Record<string, unknown> | null;
  error: GwRpcError | null;
  trace_id: string | null;
}

export const GW_KEY_STATUS: Record<GwKey["status"], { label: string; color: string }> = {
  active: { label: "正常", color: "success" },
  blocked: { label: "已停用", color: "default" },
};

export const GW_CLIENT_TYPE: Record<string, string> = { Agent: "智能体" };

export interface GwServiceIn {
  kind: GwKind;
  name: string;
  display_name: string;
  description: string;
  visibility: GwVisibility;
  connection: Record<string, unknown>;
  credential: GwCredentialIn;
  pass_identity: boolean;
  /** REST：「GET /path」；gRPC：「包.服务/方法」；MCP 代理不用挑 */
  selected: string[];
  /** gRPC 上传的 FileDescriptorSet（base64） */
  descriptor?: string | null;
}

export interface GwToolIn {
  name: string;
  description: string;
  effect: GwEffect;
  mapping: GwRestMapping;
}

// ═══════════════════════════════════════════════ 查询

const ROOT = "mcpgw";
const k = {
  me: [ROOT, "me"] as const,
  services: [ROOT, "services"] as const,
  service: (name: string) => [ROOT, "service", name] as const,
  grants: (name: string) => [ROOT, "grants", name] as const,
  keyOptions: [ROOT, "key-options"] as const,
  myKey: [ROOT, "keys", "me"] as const,
  keys: (type: string) => [ROOT, "keys", "list", type] as const,
  keyGrants: (id: string) => [ROOT, "keys", "grants", id] as const,
  clientTypes: [ROOT, "client-types"] as const,
  users: (q: string) => [ROOT, "users", q] as const,
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

export function useGwGrants(name: string, enabled: boolean) {
  return useQuery({
    queryKey: k.grants(name),
    queryFn: () => gw.get<GwServerGrants>(`/api/v1/services/${name}/grants`),
    enabled,
  });
}

/** 授权时可选的 Key（所有者、管理员） */
export function useGwKeyOptions(enabled: boolean) {
  return useQuery({
    queryKey: k.keyOptions,
    queryFn: () => gw.get<GwKeyRef[]>("/api/v1/key-options"),
    enabled,
  });
}

export function useGwMyKey() {
  return useQuery({ queryKey: k.myKey, queryFn: () => gw.get<GwKey | null>("/api/v1/keys/me") });
}

export function useGwKeys(type: "user" | "client", enabled: boolean) {
  return useQuery({
    queryKey: k.keys(type),
    queryFn: () => gw.get<GwKey[]>("/api/v1/keys", { key_type: type }),
    enabled,
  });
}

/** 这把 Key 在私有服务上的权限（现查用户中心，不走缓存） */
export function useGwKeyGrants(id: string | undefined) {
  return useQuery({
    queryKey: k.keyGrants(id ?? ""),
    queryFn: () => gw.get<GwServiceScope[]>(`/api/v1/keys/${id}/grants`),
    enabled: !!id,
  });
}

export function useGwClientTypes(enabled: boolean) {
  return useQuery({
    queryKey: k.clientTypes,
    queryFn: () => gw.get<{ code: string; apps: string[] }[]>("/api/v1/client-types"),
    enabled,
    staleTime: 300_000,
  });
}

export function useGwUsers(q: string) {
  return useQuery({
    queryKey: k.users(q),
    queryFn: () => gw.get<{ id: string; name: string; account?: string }[]>("/api/v1/users", { q }),
    enabled: q.trim().length > 0,
  });
}

// ═══════════════════════════════════════════════ 写

/** 写完一律刷新网关的全部查询：服务、工具、授权、Key 互相牵连，逐个维护失效关系不值得 */
function useDone() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: [ROOT] });
    // 编辑器的工具目录来自 Atlas（读网关的元数据，有缓存）
    void qc.invalidateQueries({ queryKey: ["tools"] });
  };
}

function useGwMutation<I, O>(fn: (input: I) => Promise<O>) {
  const done = useDone();
  return useMutation({ mutationFn: fn, onSuccess: done });
}

/** 登记前发现工具（不写库） */
export function useGwDiscover() {
  return useMutation({
    mutationFn: (body: Pick<GwServiceIn, "kind" | "connection" | "credential" | "descriptor">) =>
      gw.post<GwDiscovered>("/api/v1/discover", body),
  });
}

export function useCreateGwService() {
  return useGwMutation((body: GwServiceIn) => gw.post<GwServiceDetail>("/api/v1/services", body));
}

export function useGwServiceActions(name: string) {
  const base = `/api/v1/services/${name}`;
  return {
    patch: useGwMutation((body: { display_name?: string; description?: string; visibility?: GwVisibility }) =>
      gw.patch<GwServiceDetail>(base, body),
    ),
    connection: useGwMutation((body: { connection: Record<string, unknown>; pass_identity: boolean }) =>
      gw.put<GwServiceDetail>(`${base}/connection`, body),
    ),
    credential: useGwMutation((body: GwCredentialIn) => gw.put<GwServiceDetail>(`${base}/credential`, body)),
    setEnabled: useGwMutation((on: boolean) => gw.post<GwServiceDetail>(`${base}/${on ? "enable" : "disable"}`)),
    refresh: useGwMutation(() => gw.post<GwServiceDetail>(`${base}/refresh`)),
    test: useMutation({ mutationFn: () => gw.post<{ ok: boolean; detail: string }>(`${base}/test`) }),
    rediscover: useMutation({
      mutationFn: (descriptor: string | null) => gw.post<GwDiscovered>(`${base}/rediscover`, { descriptor }),
    }),
    importTools: useGwMutation((body: { selected: string[]; descriptor?: string | null }) =>
      gw.post<string[]>(`${base}/import`, body),
    ),
  };
}

export function useGwToolActions(service: string) {
  const base = `/api/v1/services/${service}/tools`;
  return {
    add: useGwMutation((body: GwToolIn) => gw.post<GwTool>(base, body)),
    update: useGwMutation(
      (input: { tool: string; description?: string; effect?: GwEffect; mapping?: GwRestMapping }) => {
        const { tool, ...body } = input;
        return gw.patch<GwTool>(`${base}/${tool}`, body);
      },
    ),
    remove: useGwMutation((tool: string) => gw.del(`${base}/${tool}`)),
    setEnabled: useGwMutation((input: { tool: string; on: boolean }) =>
      gw.post<GwTool>(`${base}/${input.tool}/${input.on ? "enable" : "disable"}`),
    ),
    // 试调不改网关的数据，不用刷新
    call: useMutation({
      mutationFn: (input: { tool: string; key_id: string; arguments: Record<string, unknown> }) =>
        gw.post<GwTryOut>(`${base}/${input.tool}/call`, { key_id: input.key_id, arguments: input.arguments }),
    }),
  };
}

export function useGwGrantActions(service: string) {
  const base = `/api/v1/services/${service}`;
  return {
    set: useGwMutation((input: { key_id: string; scope: "all" | "tools"; tools: string[] }) =>
      gw.put<GwServerGrants>(`${base}/grants/${input.key_id}`, { scope: input.scope, tools: input.tools }),
    ),
    revoke: useGwMutation((keyId: string) => gw.del<GwServerGrants>(`${base}/grants/${keyId}`)),
    addOwner: useGwMutation((userId: string) => gw.post<GwServerGrants>(`${base}/owners`, { user_id: userId })),
    removeOwner: useGwMutation((userId: string) => gw.del<GwServerGrants>(`${base}/owners/${userId}`)),
  };
}

export function useGwMyKeyActions() {
  return {
    apply: useGwMutation(() => gw.post<GwKeySecret>("/api/v1/keys/me")),
    reset: useGwMutation(() => gw.post<GwKeySecret>("/api/v1/keys/me/reset")),
    setBlocked: useGwMutation((blocked: boolean) => gw.post<GwKey>(`/api/v1/keys/me/${blocked ? "block" : "unblock"}`)),
    remove: useGwMutation(() => gw.del("/api/v1/keys/me")),
  };
}

export function useGwKeyActions() {
  return {
    createClient: useGwMutation((body: { client_type: string; client_code: string; name: string }) =>
      gw.post<GwKey>("/api/v1/client-keys", body),
    ),
    act: useGwMutation((input: { id: string; action: "reset" | "block" | "unblock" }) =>
      gw.post<GwKey>(`/api/v1/keys/${input.id}/${input.action}`),
    ),
    remove: useGwMutation((id: string) => gw.del(`/api/v1/keys/${id}`)),
    /** 一次给一把 Key 配多个服务：只改列出来的；scope none = 收回 */
    setGrants: useGwMutation(
      (input: { id: string; grants: { service: string; scope: "all" | "tools" | "none"; tools: string[] }[] }) =>
        gw.put<GwServiceScope[]>(`/api/v1/keys/${input.id}/grants`, { grants: input.grants }),
    ),
  };
}
