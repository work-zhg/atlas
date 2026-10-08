"use client";

/**
 * 跨会话记忆（记忆设计 §10）：浏览与删除单条。
 * ★ 只属于当前用户 —— 身份由服务端取，前端不传 user_id。
 */
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, http } from "@/lib/http";
import { qk } from "./keys";
import type { Memory, Thread } from "./types";

const MEMORIES = ["memories"] as const;

export function useMemories() {
  return useQuery({
    queryKey: MEMORIES,
    queryFn: () => http.get<{ data: Memory[] }>("/v1/memories", { limit: 200 }),
    // 503 = 没开记忆：重试没有意义
    retry: (n, err) => !(err instanceof ApiError && err.status === 503) && n < 2,
  });
}

export function useDeleteMemory() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => http.del<void>(`/v1/memories/${id}`),
    onSuccess: (_, id) =>
      qc.setQueryData<{ data: Memory[] }>(MEMORIES, (old) =>
        old ? { data: old.data.filter((m) => m.id !== id) } : old,
      ),
  });
}

/**
 * 记忆的来源会话。★ 按 id 逐个取而不是用会话列表：列表只有最近 30 个，
 * 记忆可能来自更早的会话。已删除的会话返回 null（记忆还在，出处没了）。
 */
export function useSourceThreads(ids: string[]) {
  return useQueries({
    queries: ids.map((id) => ({
      queryKey: qk.threads.detail(id),
      queryFn: async () => {
        try {
          return await http.get<Thread>(`/v1/threads/${id}`);
        } catch (err) {
          if (err instanceof ApiError && err.status === 404) return null;
          throw err;
        }
      },
      staleTime: 60_000,
    })),
  });
}
