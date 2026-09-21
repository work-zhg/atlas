"use client";

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { http } from "@/lib/http";
import { qk } from "./keys";
import type { Message, Page, Thread, ThreadCreate } from "./types";

/** 会话列表 —— keyset 游标分页（文档 §11，不用 OFFSET）。 */
export function useThreads(status?: string) {
  return useInfiniteQuery({
    queryKey: qk.threads.list(status),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      http.get<Page<Thread>>("/v1/threads", { status, cursor: pageParam, limit: 30 }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export function useThread(threadId: string | undefined) {
  return useQuery({
    queryKey: qk.threads.detail(threadId ?? ""),
    queryFn: () => http.get<Thread>(`/v1/threads/${threadId}`),
    enabled: Boolean(threadId),
  });
}

/**
 * 消息列表 —— 后端**倒序**返回（最新在前），游标向更早翻。
 * 渲染前要反转成时间正序；见 flattenMessages。
 */
export function useMessages(threadId: string | undefined) {
  return useInfiniteQuery({
    queryKey: qk.threads.messages(threadId ?? ""),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      http.get<Page<Message>>(`/v1/threads/${threadId}/messages`, {
        cursor: pageParam,
        limit: 50,
      }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: Boolean(threadId),
  });
}

/** 倒序分页的多页数据 → 时间正序的扁平数组。 */
export function flattenMessages(pages: Page<Message>[] | undefined): Message[] {
  if (!pages) return [];
  // 页本身是「越往后越早」，页内是「越往后越早」，整体反转即得正序
  return pages.flatMap((p) => p.data).reverse();
}

export function useCreateThread() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ThreadCreate) => http.post<Thread>("/v1/threads", body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.threads.all }),
  });
}

export function useRenameThread(threadId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (title: string) =>
      http.patch<Thread>(`/v1/threads/${threadId}`, { title }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.threads.all }),
  });
}

export function useDeleteThread() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (threadId: string) => http.del<void>(`/v1/threads/${threadId}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.threads.all }),
  });
}
