"use client";

/**
 * 技能应用（atlas-skill，用户中心里的独立应用「技能」）：技能的上传 / 审查 / 发布 / 下架。
 * 浏览器经网关同源访问（lib/http.ts 的 SKILL_API_BASE）。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { skillHttp } from "@/lib/http";
import type { AuditEntry, ManagedSkill, ManagedSkillVersion, SkMe } from "./skill-types";
import { qk } from "./keys";

export function useSkMe() {
  return useQuery({
    queryKey: qk.skillApp.me,
    queryFn: () => skillHttp.get<SkMe>("/api/v1/me"),
    staleTime: 60_000,
    // 没部署 / 连不上 / 不在可访问范围：导航里不显示「技能」，不反复重试
    retry: false,
  });
}

export function useSkills() {
  return useQuery({
    queryKey: qk.skillApp.skills,
    queryFn: () => skillHttp.get<ManagedSkill[]>("/api/v1/skills"),
    staleTime: 15_000,
    // 技能应用没起来时不要反复重试刷屏 —— 页面上直接说明
    retry: 1,
  });
}

/** 包内文件。published 用 (slug, version)，待审查的用 upload id。 */
export function useSkillFile(version: ManagedSkillVersion | undefined, path: string | undefined) {
  const url =
    version && path
      ? version.version != null && version.status !== "pending_review"
        ? `/api/v1/skills/${version.slug}/versions/${version.version}/files/${path}`
        : `/api/v1/skill-uploads/${version.id}/files/${path}`
      : "";
  return useQuery({
    queryKey: qk.skillApp.file(url),
    queryFn: () => skillHttp.text(url),
    enabled: !!url,
    staleTime: Infinity, // 已发布的内容不可变
  });
}

function useInvalidateSkills() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: qk.skillApp.skills });
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
      return skillHttp.post<ManagedSkillVersion>("/api/v1/skills", form);
    },
    onSuccess: done,
  });
}

export function useReviewSkill() {
  const done = useInvalidateSkills();
  return useMutation({
    mutationFn: (input: { uploadId: string; approve: boolean; note: string }) =>
      skillHttp.post<ManagedSkillVersion>(
        `/api/v1/skill-uploads/${input.uploadId}/${input.approve ? "approve" : "reject"}`,
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
      skillHttp.post<ManagedSkillVersion>(
        `/api/v1/skills/${input.slug}/versions/${input.version}/${input.action}`,
        input.reason ? { reason: input.reason } : {},
      ),
    onSuccess: done,
  });
}

export function useAudit(targetId: string | undefined) {
  return useQuery({
    queryKey: qk.skillApp.audit(targetId ?? ""),
    queryFn: () => skillHttp.get<AuditEntry[]>("/api/v1/audit", { target_id: targetId, limit: 30 }),
    enabled: !!targetId,
  });
}
