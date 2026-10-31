"use client";

/**
 * 上下文管理（doc/context-management-design.html §10 / §16）：
 * 会话的上下文用量与构成、手动压缩；持久指令（个人 / 平台）。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ContextUsageData } from "@atlas/chat-ui/src/lib/events";

import { http } from "@/lib/http";

export interface ThreadContextStatus {
  usage: ContextUsageData | null;
  usage_at: string | null;
  summary: {
    present: boolean;
    chars: number;
    upto: string | null;
    compact_count: number;
  };
  compaction_failures: number;
  compaction_disabled: boolean;
}

export function useThreadContext(threadId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: ["thread-context", threadId],
    queryFn: () =>
      http.get<ThreadContextStatus>(`/v1/threads/${threadId}/context`),
    enabled: Boolean(threadId) && enabled,
    staleTime: 10_000,
  });
}

export function useCompactThread(threadId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (focus: string) =>
      http.post<{
        summary: string;
        messages_summarized: number;
        model: string;
      }>(`/v1/threads/${threadId}/compact`, { focus }),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["thread-context", threadId] }),
  });
}

export interface InstructionOut {
  scope: "user" | "platform";
  content: string;
  updated_at: string | null;
  updated_by: string | null;
  max_chars: number;
}

export function useInstruction(scope: "me" | "platform") {
  return useQuery({
    queryKey: ["instruction", scope],
    queryFn: () => http.get<InstructionOut>(`/v1/instructions/${scope}`),
    retry: 1,
  });
}

export function useSaveInstruction(scope: "me" | "platform") {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (content: string) =>
      http.put<InstructionOut>(`/v1/instructions/${scope}`, { content }),
    onSuccess: (data) => qc.setQueryData(["instruction", scope], data),
  });
}
