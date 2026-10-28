"use client";

/**
 * 模型管理平台（atlas-modelgateway，agent-platform/doc/design/model-design.html）的管理端接口。
 *
 *   模型         登记（写 LiteLLM + 平台记录）、编辑、重新登记、纳入 LiteLLM 直配的、删除   管理员
 *   授权         私有模型授权给用户、部门（可含下级）                                         管理员
 *   我的用户 Key 本人申请、分配模型、重置、停用 / 启用、删除                                 所有人
 *   客户端 Key   应用调接口创建；管理员分配模型、重置、停用 / 启用、删除，也能新建（平台服务） 管理员
 *
 * ★ 平台不在调用路径上：调用日志、花费在 LiteLLM 管理界面看。
 * ★ 浏览器经网关同源调它（lib/http.ts 的 MODEL_GATEWAY_API_BASE），身份由网关写入。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { modelGatewayHttp as gw } from "@/lib/http";

// ═══════════════════════════════════════════════ 类型（平台 api/schemas.py）

export type MgVisibility = "public" | "private";

export interface MgMe {
  uid: string;
  can_manage: boolean;
  /** 用户 Key 调 LiteLLM 的地址；空 = 还没对集群外开放 */
  litellm_url: string | null;
}

export interface MgProvider {
  code: string;
  name: string;
  /** 必须填 API Base（自部署、OpenAI 兼容） */
  needs_base: boolean;
  /** 供应商 Key 可以不填（自部署） */
  key_optional: boolean;
}

export interface MgGrant {
  id: string;
  subject_type: "user" | "dept" | string;
  subject_id: string;
  subject_name: string;
  permission: string;
  include_sub: boolean;
}

export interface MgModel {
  /** 平台记录的 id；LiteLLM 直配的为空 */
  uuid: string | null;
  name: string;
  display_name: string;
  provider: string;
  /** LiteLLM 直配的为空 */
  visibility: MgVisibility | null;
  description: string;
  source: "platform" | "litellm";
  /** ok = 可调用；missing = LiteLLM 中缺失；unknown = LiteLLM 不可达 */
  status: "ok" | "missing" | "unknown";
  /** 平台在 LiteLLM 里建的部署（能改地址、凭据、规格价格）；纳入平台的为 false */
  managed: boolean;
  /** 直配模型的来源：db = LiteLLM 管理界面；config = 配置文件 */
  origin: "db" | "config" | null;
  deployments: number;
  api_base: string | null;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  /** 美元 / 百万 token */
  input_price: number | null;
  output_price: number | null;
  /** 管理员看：私有模型的授权 */
  grants: MgGrant[] | null;
  /** 普通用户看：public = 公有；granted = 授权给了你或你所在的部门 */
  access: "public" | "granted" | null;
  created_by: string | null;
  created_at: string | null;
}

export interface MgModelList {
  data: MgModel[];
  litellm_reachable: boolean;
}

export interface MgGrantIn {
  subject_type: "user" | "dept";
  subject_id: string;
  include_sub: boolean;
}

/** 只交给 LiteLLM 的部分 */
export interface MgRouteIn {
  api_base?: string | null;
  /** 编辑时留空 = 不改 */
  api_key?: string | null;
  max_input_tokens?: number | null;
  max_output_tokens?: number | null;
  input_price?: number | null;
  output_price?: number | null;
}

export interface MgModelIn extends MgRouteIn {
  name: string;
  display_name: string;
  provider: string;
  visibility: MgVisibility;
  description: string;
  grants: MgGrantIn[];
}

export type MgKeyStatus = "pending" | "active" | "blocked";

export interface MgKeyModel {
  name: string;
  /** ok；revoked = 授权已撤销（或不再可选）；missing = LiteLLM 中缺失；deleted = 模型已删除 */
  state: "ok" | "revoked" | "missing" | "deleted";
}

export interface MgKey {
  uuid: string;
  key_type: "user" | "client";
  user_uuid: string | null;
  client_type: string | null;
  client_code: string | null;
  name: string;
  status: MgKeyStatus;
  /** sk-…后四位；待分配时为空 */
  masked: string | null;
  models: MgKeyModel[];
  /** 有效模型：同步给 LiteLLM 的 */
  effective: string[];
  /** 已分配过，但有效模型算下来为空（LiteLLM 里停用） */
  no_models: boolean;
  sync_error: string | null;
  created_by: string;
  created_at: string;
  rotated_at: string | null;
}

export interface MgKeyWithSecret extends MgKey {
  /** 原文：只在申请、重置时返回一次 */
  secret: string | null;
}

export interface MgClientType {
  code: string;
  apps: string[];
}

export const MG_KEY_STATUS: Record<MgKeyStatus, { label: string; color: string }> = {
  pending: { label: "待分配模型", color: "warning" },
  active: { label: "正常", color: "success" },
  blocked: { label: "已停用", color: "default" },
};

export const MG_KEY_MODEL_STATE: Record<MgKeyModel["state"], string> = {
  ok: "",
  revoked: "授权已撤销",
  missing: "LiteLLM 中缺失，暂不可调用",
  deleted: "模型已删除",
};

/** 客户端类型的中文名（平台只给编码） */
export const MG_CLIENT_TYPE: Record<string, string> = { Agent: "智能体", Service: "平台服务" };

// ═══════════════════════════════════════════════ 查询

const k = {
  me: ["modelgw", "me"] as const,
  providers: ["modelgw", "providers"] as const,
  models: ["modelgw", "models"] as const,
  myKey: ["modelgw", "keys", "me"] as const,
  clientTypes: ["modelgw", "client-types"] as const,
  clientKeys: ["modelgw", "client-keys"] as const,
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

export function useMgProviders(enabled: boolean) {
  return useQuery({
    queryKey: k.providers,
    queryFn: () => gw.get<MgProvider[]>("/api/v1/providers"),
    staleTime: 10 * 60_000,
    enabled,
  });
}

export function useMgModels() {
  return useQuery({
    queryKey: k.models,
    queryFn: () => gw.get<MgModelList>("/api/v1/models"),
    staleTime: 15_000,
    retry: 1,
  });
}

export function useMgMyKey() {
  return useQuery({ queryKey: k.myKey, queryFn: () => gw.get<MgKey | null>("/api/v1/keys/me") });
}

export function useMgClientTypes(enabled: boolean) {
  return useQuery({
    queryKey: k.clientTypes,
    queryFn: () => gw.get<MgClientType[]>("/api/v1/client-types"),
    staleTime: 10 * 60_000,
    enabled,
  });
}

export function useMgClientKeys(enabled: boolean) {
  return useQuery({
    queryKey: k.clientKeys,
    queryFn: () => gw.get<MgKey[]>("/api/v1/client-keys"),
    enabled,
  });
}

// ═══════════════════════════════════════════════ 写

/** 模型变了：模型列表、两类 Key 的模型状态、Atlas 编辑器的模型列表都要刷新 */
function useRefresh() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: ["modelgw"] });
    void qc.invalidateQueries({ queryKey: ["models"] });
  };
}

export function useTestMgConnection() {
  return useMutation({
    mutationFn: (body: MgRouteIn & { name: string; provider: string }) =>
      gw.post<{ ok: boolean; message: string }>("/api/v1/models/test-connection", body),
  });
}

export function useSaveMgModel() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (
      input:
        | { kind: "new"; body: MgModelIn }
        | { kind: "edit"; id: string; body: Omit<MgModelIn, "name" | "grants"> }
        | { kind: "reregister"; id: string; body: MgRouteIn & { provider: string } }
        | {
            kind: "adopt";
            body: Pick<MgModelIn, "name" | "display_name" | "visibility" | "description" | "grants">;
          },
    ) => {
      switch (input.kind) {
        case "new":
          return gw.post<MgModel>("/api/v1/models", input.body);
        case "edit":
          return gw.put<MgModel>(`/api/v1/models/${input.id}`, input.body);
        case "reregister":
          return gw.post<MgModel>(`/api/v1/models/${input.id}/reregister`, input.body);
        case "adopt":
          return gw.post<MgModel>("/api/v1/models/adopt", input.body);
      }
    },
    onSuccess: refresh,
  });
}

export function useDeleteMgModel() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (id: string) =>
      gw.del<{ deleted: string; keys_updated: number }>(`/api/v1/models/${id}`),
    onSuccess: refresh,
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

export type MgKeyAction = "reset" | "block" | "unblock";

/** 我的用户 Key */
export function useMgMyKeyActions() {
  const refresh = useRefresh();
  const apply = useMutation({
    mutationFn: (models: string[]) => gw.post<MgKeyWithSecret>("/api/v1/keys/me", { models }),
    onSuccess: refresh,
  });
  const assign = useMutation({
    mutationFn: (models: string[]) => gw.put<MgKey>("/api/v1/keys/me/models", { models }),
    onSuccess: refresh,
  });
  const act = useMutation({
    mutationFn: (action: MgKeyAction) => gw.post<MgKeyWithSecret>(`/api/v1/keys/me/${action}`),
    onSuccess: refresh,
  });
  const remove = useMutation({ mutationFn: () => gw.del("/api/v1/keys/me"), onSuccess: refresh });
  return { apply, assign, act, remove };
}

/** 客户端 Key（管理员） */
export function useMgClientKeyActions() {
  const refresh = useRefresh();
  const create = useMutation({
    mutationFn: (body: { client_type: string; client_code: string; name: string }) =>
      gw.post<MgKey>("/api/v1/client-keys", body),
    onSuccess: refresh,
  });
  const assign = useMutation({
    mutationFn: (input: { id: string; models: string[] }) =>
      gw.put<MgKey>(`/api/v1/client-keys/${input.id}/models`, { models: input.models }),
    onSuccess: refresh,
  });
  const act = useMutation({
    mutationFn: (input: { id: string; action: MgKeyAction }) =>
      gw.post<MgKey>(`/api/v1/client-keys/${input.id}/${input.action}`),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (id: string) => gw.del(`/api/v1/client-keys/${id}`),
    onSuccess: refresh,
  });
  return { create, assign, act, remove };
}
