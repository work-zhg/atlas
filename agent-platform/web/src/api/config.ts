"use client";

/**
 * 配置服务（atlas-config）：技能的上传 / 审查 / 发布 / 下架，MCP 注册表与工具复核。
 * 浏览器直连（BFF 上线前，见 lib/http.ts 的 CONFIG_API_BASE）。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { configHttp } from "@/lib/http";
import type {
  AuditEntry,
  ConfigMcpServer,
  ConfigSkill,
  ConfigSkillVersion,
  McpServerIn,
  McpServerPatch,
  ToolReview,
  ToolReviewIn,
} from "./config-types";
import { qk } from "./keys";

export function useConfigSkills() {
  return useQuery({
    queryKey: qk.config.skills,
    queryFn: () => configHttp.get<ConfigSkill[]>("/config/skills"),
    staleTime: 15_000,
    // 配置服务没起来时不要反复重试刷屏 —— 页面上直接说明
    retry: 1,
  });
}

/** 包内文件。published 用 (slug, version)，待审查的用 upload id。 */
export function useSkillFile(version: ConfigSkillVersion | undefined, path: string | undefined) {
  const url =
    version && path
      ? version.version != null && version.status !== "pending_review"
        ? `/config/skills/${version.slug}/versions/${version.version}/files/${path}`
        : `/config/skill-uploads/${version.id}/files/${path}`
      : "";
  return useQuery({
    queryKey: qk.config.file(url),
    queryFn: () => configHttp.text(url),
    enabled: !!url,
    staleTime: Infinity, // 已发布的内容不可变
  });
}

function useInvalidateSkills() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: qk.config.skills });
    void qc.invalidateQueries({ queryKey: qk.catalog.skills });
  };
}

export function useUploadSkill() {
  const done = useInvalidateSkills();
  return useMutation({
    mutationFn: (input: { file: File; source: string; originUrl?: string }) => {
      const form = new FormData();
      form.append("file", input.file);
      form.append("source", input.source);
      if (input.originUrl) form.append("origin_url", input.originUrl);
      return configHttp.post<ConfigSkillVersion>("/config/skills", form);
    },
    onSuccess: done,
  });
}

export function useReviewSkill() {
  const done = useInvalidateSkills();
  return useMutation({
    mutationFn: (input: { uploadId: string; approve: boolean; note: string }) =>
      configHttp.post<ConfigSkillVersion>(
        `/config/skill-uploads/${input.uploadId}/${input.approve ? "approve" : "reject"}`,
        { note: input.note },
      ),
    onSuccess: done,
  });
}

export type SkillAction = "disable" | "enable" | "revoke" | "rescan";

export function useSkillAction() {
  const done = useInvalidateSkills();
  return useMutation({
    mutationFn: (input: { slug: string; version: number; action: SkillAction; reason?: string }) =>
      configHttp.post<ConfigSkillVersion>(
        `/config/skills/${input.slug}/versions/${input.version}/${input.action}`,
        input.reason ? { reason: input.reason } : {},
      ),
    onSuccess: done,
  });
}

export function useConfigMcpServers(enabled = true) {
  return useQuery({
    queryKey: qk.config.mcp,
    queryFn: () => configHttp.get<ConfigMcpServer[]>("/config/mcp/servers"),
    enabled,
    retry: 1,
  });
}

function useInvalidateMcp() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: qk.config.mcp });
    void qc.invalidateQueries({ queryKey: qk.catalog.mcp });
    void qc.invalidateQueries({ queryKey: qk.tools });
  };
}

export function useCreateMcpServer() {
  const done = useInvalidateMcp();
  return useMutation({
    mutationFn: (body: McpServerIn) => configHttp.post<ConfigMcpServer>("/config/mcp/servers", body),
    onSuccess: done,
  });
}

export function usePatchMcpServer() {
  const done = useInvalidateMcp();
  return useMutation({
    mutationFn: (input: { name: string; patch: McpServerPatch }) =>
      configHttp.patch<ConfigMcpServer>(`/config/mcp/servers/${input.name}`, input.patch),
    onSuccess: done,
  });
}

export function useReviewTool(server: string) {
  const done = useInvalidateMcp();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ToolReviewIn) =>
      configHttp.post<ToolReview>(`/config/mcp/servers/${server}/reviews`, body),
    onSuccess: () => {
      done();
      void qc.invalidateQueries({ queryKey: qk.catalog.mcpServer(server) });
    },
  });
}

export function useAudit(targetId: string | undefined) {
  return useQuery({
    queryKey: ["config", "audit", targetId ?? ""],
    queryFn: () => configHttp.get<AuditEntry[]>("/config/audit", { target_id: targetId, limit: 30 }),
    enabled: !!targetId,
  });
}
