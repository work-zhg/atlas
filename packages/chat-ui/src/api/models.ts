"use client";

/** 对话框模型选择器的候选：这个智能体能用的模型（按它的 Key 列出，与编辑器同一个接口）。 */
import { useQuery } from "@tanstack/react-query";

import { useAtlasHttp } from "../client";
import { qk } from "./keys";
import type { components } from "./schema";

type ModelListResponse = components["schemas"]["ModelListResponse"];

export function useAgentModels(agentId: string | undefined, enabled = true) {
  const http = useAtlasHttp();
  return useQuery({
    // ★ 与 Atlas 编辑器的 useModels 同一个 key、同一个接口：两边共用缓存
    queryKey: [...qk.models, agentId ?? ""],
    queryFn: () => http.get<ModelListResponse>("/v1/models", { agent_id: agentId }),
    enabled: Boolean(agentId) && enabled,
    // 管理员给智能体分配模型后要能看到：和编辑器一样缓 1 分钟
    staleTime: 60_000,
  });
}

/**
 * 选择器里能选的：Key 可用时它的可用模型（没接模型平台时 key 为空，按巡检结果）。
 * Key 待分配、停用时接口列的是平台上的全部模型，但这个智能体一个也用不了 —— 不给选。
 */
export function selectableModels(res: ModelListResponse | undefined) {
  if (!res || (res.key && res.key.status !== "active")) return [];
  return res.data.filter((m) => m.is_available && m.selectable);
}
