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

export function useTools() {
  return useQuery({
    queryKey: qk.tools,
    queryFn: () => http.get<{ data: ToolInfo[] }>("/v1/tools"),
    staleTime: 5 * 60_000,
  });
}
