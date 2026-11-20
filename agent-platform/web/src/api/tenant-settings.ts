"use client";

/**
 * 租户设置（c4-model/agent/code-tenant-settings.html；多租户设计 §3.3）：租户管理员管自己租户的地方。
 *
 *   GET  /v1/tenant-settings                 默认智能体、每人上限，只读的份额和用量        登录即可看
 *   GET  知识库 /v1/capacity                  本租户知识库文件的份额和用量                  登录即可看
 *   *    /v1/service-models[/{purpose}]       系统功能（会话标题、记忆补记、判断器）用哪个模型  settings:manage
 *
 * ★ 份额由运营中心定，这里只读；每人上限在「配置项」里改（/v1/tenant-settings/configs）。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { http, knowledgeHttp } from "@/lib/http";

/** 租户管理员的操作码（Atlas 只剩这一个管理操作码） */
export const SETTINGS_MANAGE = "settings:manage";

export interface TenantSettings {
  default_agent: string | null;
  /** 0 = 不限 */
  per_person: { running_runs: number; sandboxes: number; storage_bytes: number };
  /** 本租户份额：null = 不限；is_default = 运营中心没单独定，用的系统默认值 */
  quota: { running_runs: number | null; sandboxes: number | null; storage_bytes: number | null; is_default: boolean };
  usage: { running_runs: number; sandboxes: number; storage_bytes: number };
  can_manage: boolean;
}

export interface KbCapacity {
  quota_bytes: number | null;
  used_bytes: number;
  is_default: boolean;
}

export interface ServiceModel {
  purpose: string;
  name: string;
  client_type: string | null;
  client_code: string | null;
  model: string | null;
  ready: boolean;
  problem: string | null;
}

const k = {
  settings: ["tenant-settings"] as const,
  kb: ["tenant-settings", "kb-capacity"] as const,
  serviceModels: ["tenant-settings", "service-models"] as const,
};

export function useTenantSettings() {
  return useQuery({ queryKey: k.settings, queryFn: () => http.get<TenantSettings>("/v1/tenant-settings") });
}

export function useKbCapacity() {
  // 知识库没部署 / 连不上：卡片里不显示这一项，不反复重试
  return useQuery({ queryKey: k.kb, queryFn: () => knowledgeHttp.get<KbCapacity>("/v1/capacity"), retry: false });
}

export function useServiceModels(enabled = true) {
  return useQuery({
    queryKey: k.serviceModels,
    queryFn: () => http.get<ServiceModel[]>("/v1/service-models"),
    enabled,
  });
}

export function useServiceModelActions() {
  const qc = useQueryClient();
  const refresh = () => void qc.invalidateQueries({ queryKey: k.serviceModels });
  const set = useMutation({
    mutationFn: (input: { purpose: string; client_type: string; client_code: string; model: string }) =>
      http.put<ServiceModel>(`/v1/service-models/${input.purpose}`, {
        client_type: input.client_type,
        client_code: input.client_code,
        model: input.model,
      }),
    onSuccess: refresh,
  });
  const clear = useMutation({
    mutationFn: (purpose: string) => http.del(`/v1/service-models/${purpose}`),
    onSuccess: refresh,
  });
  return { set, clear };
}
