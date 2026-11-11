"use client";

/** 回答反馈（有帮助 / 没帮助）。一个人对一条回答只有一条，再点就是改；点已选中的那个是撤回。 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useAtlasHttp } from "../client";
import { qk } from "./keys";

export type Rating = "up" | "down";

export interface Feedback {
  message_id: string;
  rating: Rating;
  reasons: string[];
  comment: string;
}

/** 我对这条会话里各条回答的反馈，按消息 id */
export function useThreadFeedback(threadId: string, enabled = true) {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: qk.threads.feedback(threadId),
    queryFn: () => http.get<{ data: Feedback[] }>(`/v1/threads/${threadId}/feedback`),
    select: (res) => Object.fromEntries(res.data.map((f) => [f.message_id, f])) as Record<string, Feedback>,
    enabled: Boolean(threadId) && enabled,
    staleTime: 60_000,
  });
}

export interface SetFeedbackInput {
  messageId: string;
  /** null = 撤回 */
  rating: Rating | null;
  reasons?: string[];
  comment?: string;
}

export function useSetFeedback(threadId: string) {
  const http = useAtlasHttp();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ messageId, rating, reasons = [], comment = "" }: SetFeedbackInput): Promise<void> => {
      const url = `/v1/threads/${threadId}/messages/${messageId}/feedback`;
      if (rating === null) await http.del<void>(url);
      else await http.put<Feedback>(url, { rating, reasons, comment });
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.threads.feedback(threadId) }),
  });
}
