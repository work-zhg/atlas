"use client";

/**
 * 浏览器的接口（c4-model/agent/browser/design-browser.html）：接管、实时画面、我的登录。
 *
 *   接管      打开 → 交还 / 不处理。只有会话的创建人能处理（浏览器里是他的登录）
 *   画面      WS /v1/threads/{会话}/browser/view：同源，网关照常校验登录；浏览器没在跑时带着原因关掉
 *   我的登录  存了哪些站点（没有内容，只有站点和时间）；删除只影响以后
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useAtlasClient, useAtlasHttp } from "../client";
import { apiUrl } from "../lib/http";
import { qk } from "./keys";

export interface TakeoverLogin {
  saved: boolean;
  sites: string[];
}

export interface TakeoverResult {
  approval_id: string;
  run_id: string;
  thread_id: string;
  status: "opened" | "approved" | "rejected";
  login?: TakeoverLogin | null;
}

export interface BrowserLogin {
  site: string;
  captured_at: string;
  captured_from?: string | null;
  expires_at?: string | null;
  expired: boolean;
}

interface TakeoverInput {
  runId: string;
  approvalId: string;
  note?: string;
}

/** 打开接管：确保浏览器在（被回收了要重建，几十秒），开接管窗口。可以重复点。 */
export function useOpenTakeover() {
  const http = useAtlasHttp();
  return useMutation({
    mutationFn: ({ runId, approvalId }: TakeoverInput) =>
      http.post<TakeoverResult>(`/v1/runs/${runId}/takeovers/${approvalId}/open`),
  });
}

function useDecideTakeover(action: "hand-back" | "decline") {
  const qc = useQueryClient();
  const http = useAtlasHttp();
  return useMutation({
    mutationFn: ({ runId, approvalId, note }: TakeoverInput) =>
      http.post<TakeoverResult>(`/v1/runs/${runId}/takeovers/${approvalId}/${action}`, {
        note: note?.trim() || null,
      }),
    onSuccess: (_d, vars) => {
      void qc.invalidateQueries({ queryKey: qk.runs.detail(vars.runId) });
      void qc.invalidateQueries({ queryKey: qk.runs.approvals(vars.runId, vars.approvalId) });
      void qc.invalidateQueries({ queryKey: qk.browser.logins });
    },
  });
}

/** 交还：先存这次登录的站点，再让智能体接着做 */
export const useHandBackTakeover = () => useDecideTakeover("hand-back");
/** 不处理：智能体得知用户没有接管和原因 */
export const useDeclineTakeover = () => useDecideTakeover("decline");

/** 这个会话的浏览器在不在跑（在跑才有画面）。没开浏览器能力的部署回 409，当作不在跑 */
export function useBrowserStatus(threadId: string | undefined, enabled = true) {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: qk.browser.status(threadId ?? ""),
    queryFn: async () => {
      try {
        return await http.get<{ running: boolean }>(`/v1/threads/${threadId}/browser`);
      } catch {
        return { running: false };
      }
    },
    enabled: Boolean(threadId) && enabled,
    refetchInterval: 30_000,
  });
}

export function useBrowserLogins() {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: qk.browser.logins,
    queryFn: () => http.get<{ items: BrowserLogin[] }>("/v1/browser/logins"),
    select: (r) => r.items,
  });
}

export function useForgetLogin() {
  const qc = useQueryClient();
  const http = useAtlasHttp();
  return useMutation({
    mutationFn: (site: string) => http.del<void>(`/v1/browser/logins/${encodeURIComponent(site)}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.browser.logins }),
  });
}

/** 画面的 WebSocket 地址：与接口同源（https → wss），身份由网关按登录态写入 */
export function useBrowserViewUrl(threadId: string | undefined): string | undefined {
  const { apiBase } = useAtlasClient();
  if (!threadId) return undefined;
  return apiUrl(`/v1/threads/${threadId}/browser/view`, undefined, apiBase).replace(/^http/, "ws");
}
