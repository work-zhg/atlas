"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { AnyTraceEvent, ApprovalMode } from "../lib/events";
import { useAtlasHttp } from "../client";
import { applyEvents, initialRunState, type RunState } from "../lib/run-reducer";
import { qk } from "./keys";
import type { PendingApprovalList, Run, RunAccepted } from "./types";

export interface SendMessageInput {
  threadId: string;
  content: { type: string; text?: string; [k: string]: unknown }[];
  /** 幂等键：网络抖动重发时不会变成两条消息（§11.2）。调用方每次发送生成一个。 */
  idempotencyKey?: string;
  /** 用户在选择器里选的审批模式；不传 = 沿用会话记住的模式（没有就用智能体默认） */
  approvalMode?: ApprovalMode;
}

export function useSendMessage() {
  const qc = useQueryClient();
  const http = useAtlasHttp();
  return useMutation({
    mutationFn: ({ threadId, content, idempotencyKey, approvalMode }: SendMessageInput) =>
      http.post<RunAccepted>(
        `/v1/threads/${threadId}/runs`,
        approvalMode ? { content, approval_mode: approvalMode } : { content },
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
  const http = useAtlasHttp();
  return useQuery({
    queryKey: qk.runs.detail(runId ?? ""),
    queryFn: () => http.get<Run>(`/v1/runs/${runId}`),
    enabled: Boolean(runId) && enabled,
  });
}

/**
 * 一轮已结束后的过程轨迹（含子 run），归约成与实时区同形的 RunState。
 *
 * ★ 只给**已结束**的轮次用：结束后的轨迹不会再变，所以 staleTime = Infinity。
 *   进行中的轮次不要调 —— 会把一份残缺的轨迹永久缓存下来。
 * ★ 归档被清理后返回的是空事件列表，归约结果的 timeline 为空，调用方据此
 *   退回只显示消息正文。
 */
export function useRunTrace(runId: string | undefined, enabled = true) {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: qk.runs.trace(runId ?? ""),
    queryFn: () => http.get<{ data: AnyTraceEvent[] }>(`/v1/runs/${runId}/trace`),
    select: (res): RunState => applyEvents(initialRunState, res.data),
    enabled: Boolean(runId) && enabled,
    staleTime: Infinity,
  });
}

/**
 * 某条审批此刻是否还在等人 —— 以库为准。
 *
 * ★ 事件流回放只能说明「曾经要求过审批」，说明不了「现在还在等」：事件契约里
 *   没有「已决策」事件。刷新页面后只凭回放弹窗，就会把早已批过 / 拒过的审批
 *   再弹一次。这个端点（GET /runs/{id}/approvals）本就是为刷新恢复设计的。
 * ★ 可靠的前提是「事件发出时审批行已在库里」—— native 与 acp 两条路都守着
 *   这条不变式（tests/test_acp_approval_order.py）。
 */
export function useApprovalStillPending(runId: string | undefined, approvalId: string | undefined) {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: qk.runs.approvals(runId ?? "", approvalId ?? ""),
    queryFn: () => http.get<PendingApprovalList>(`/v1/runs/${runId}/approvals`),
    select: (list) => list.data.some((a) => a.id === approvalId),
    enabled: Boolean(runId && approvalId),
    staleTime: 0,
  });
}

export interface DecideApprovalInput {
  runId: string;
  approvalId: string;
  decision: "approved" | "rejected";
  /** 批准计划时必填：用户选的执行模式（须在智能体允许的范围里，否则 400） */
  approvalMode?: Exclude<ApprovalMode, "plan">;
  /** 拒绝原因 / 计划的修改意见，作为工具结果交给智能体 */
  feedback?: string;
  /** 问题（kind=question）的答案，与问题一一对应；某题两项都空 = 跳过，交给智能体定 */
  answers?: { selected: string[]; other: string | null }[];
}

/** 提交人工确认：工具调用、规划模式提交的计划，或回答智能体的问题。409 = 已被别处决策过。 */
export function useDecideApproval() {
  const qc = useQueryClient();
  const http = useAtlasHttp();
  return useMutation({
    mutationFn: ({
      runId,
      approvalId,
      decision,
      approvalMode,
      feedback,
      answers,
    }: DecideApprovalInput) =>
      http.post<{ decision: string }>(`/v1/runs/${runId}/approvals/${approvalId}`, {
        decision,
        ...(approvalMode ? { approval_mode: approvalMode } : {}),
        ...(feedback ? { feedback } : {}),
        ...(answers ? { answers } : {}),
      }),
    onSuccess: (_d, vars) => {
      void qc.invalidateQueries({ queryKey: qk.runs.detail(vars.runId) });
      void qc.invalidateQueries({ queryKey: qk.runs.approvals(vars.runId, vars.approvalId) });
    },
  });
}

export function useCancelRun() {
  const qc = useQueryClient();
  const http = useAtlasHttp();
  return useMutation({
    mutationFn: (runId: string) => http.post<Run>(`/v1/runs/${runId}/cancel`),
    onSuccess: (run) => {
      qc.setQueryData(qk.runs.detail(run.id), run);
    },
  });
}
