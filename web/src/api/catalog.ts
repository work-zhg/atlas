"use client";

/**
 * 技能 / MCP 的**运行时**视图（server /v1/skills、/v1/mcp/*）：
 * 被谁引用、用得怎样、MCP server 此刻的工具定义。内容与审查在配置服务（api/config.ts）。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { http } from "@/lib/http";
import { qk } from "./keys";
import type {
  McpServerDetail,
  McpServerListOut,
  SkillListOut,
  SkillReference,
  SkillUsageOut,
} from "./types";

export function useSkillCatalog() {
  return useQuery({
    queryKey: qk.catalog.skills,
    queryFn: () => http.get<SkillListOut>("/v1/skills"),
    staleTime: 30_000,
  });
}

export function useSkillReferences(slug: string | undefined) {
  return useQuery({
    queryKey: qk.catalog.skillRefs(slug ?? ""),
    queryFn: () => http.get<{ data: SkillReference[] }>(`/v1/skills/${slug}/references`),
    enabled: !!slug,
  });
}

export function useSkillUsage(days = 7) {
  return useQuery({
    queryKey: qk.catalog.skillUsage(days),
    queryFn: () => http.get<SkillUsageOut>("/v1/skills/usage", { days }),
    staleTime: 60_000,
  });
}

export function useMcpServers() {
  return useQuery({
    queryKey: qk.catalog.mcp,
    queryFn: () => http.get<McpServerListOut>("/v1/mcp/servers"),
    staleTime: 30_000,
  });
}

export function useMcpServer(name: string | undefined) {
  return useQuery({
    queryKey: qk.catalog.mcpServer(name ?? ""),
    queryFn: () => http.get<McpServerDetail>(`/v1/mcp/servers/${name}`),
    enabled: !!name,
  });
}

/** 强制重新发现。后端同一 server 10 秒内只执行一次（429）。 */
export function useRefreshMcpServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => http.post<McpServerDetail>(`/v1/mcp/servers/${name}/refresh`),
    onSuccess: (detail) => {
      qc.setQueryData(qk.catalog.mcpServer(detail.name), detail);
      void qc.invalidateQueries({ queryKey: qk.catalog.mcp, exact: true });
      void qc.invalidateQueries({ queryKey: qk.tools });
    },
  });
}
