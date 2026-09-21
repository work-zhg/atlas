"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { http } from "@/lib/http";
import { qk } from "./keys";
import type { Run, RunAccepted } from "./types";

export interface SendMessageInput {
  threadId: string;
  content: { type: string; text?: string; [k: string]: unknown }[];
  /** 幂等键：网络抖动重发时不会变成两条消息（§11.2）。调用方每次发送生成一个。 */
  idempotencyKey?: string;
}

export function useSendMessage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ threadId, content, idempotencyKey }: SendMessageInput) =>
      http.post<RunAccepted>(
        `/v1/threads/${threadId}/runs`,
        { content },
        idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
      ),
    onSuccess: (_data, vars) => {
      // 用户消息已落库，列表要刷新；会话的 updated_at / message_count 也变了
      void qc.invalidateQueries({ queryKey: qk.threads.messages(vars.threadId) });
      void qc.invalidateQueries({ queryKey: qk.threads.all });
    },
  });
}

/**
 * run 详情。文档 §10 明确要求：收到 run.finished 后回查这里取最终用量 ——
 * 事件里的 usage 可能早于执行器落库。
 */
export function useRun(runId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: qk.runs.detail(runId ?? ""),
    queryFn: () => http.get<Run>(`/v1/runs/${runId}`),
    enabled: Boolean(runId) && enabled,
  });
}

export interface DecideApprovalInput {
  runId: string;
  approvalId: string;
  decision: "approved" | "rejected";
}

/** 提交高风险工具的人工确认（§12.2）。409 = 已被别处决策过。 */
export function useDecideApproval() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, approvalId, decision }: DecideApprovalInput) =>
      http.post<{ decision: string }>(`/v1/runs/${runId}/approvals/${approvalId}`, { decision }),
    onSuccess: (_d, vars) => {
      void qc.invalidateQueries({ queryKey: qk.runs.detail(vars.runId) });
    },
  });
}

export function useCancelRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => http.post<Run>(`/v1/runs/${runId}/cancel`),
    onSuccess: (run) => {
      qc.setQueryData(qk.runs.detail(run.id), run);
    },
  });
}
