"use client";

/**
 * 跨会话记忆的管理接口（doc/memory-design.html §7，后端 /v1/memories）。
 *
 * ★ 只操作当前用户自己的记忆 —— 身份由网关写入，前端不传 user_id。
 * ★ 任何改动之后整体失效记忆相关的查询：一次写入可能同时动到几个主题（取代、移动），
 *   逐个算哪些缓存过期得不偿失。
 */
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, http } from "@/lib/http";
import { qk } from "./keys";
import type {
  MemoryBackfill,
  MemoryEntryDetail,
  MemoryEntryList,
  MemoryRevision,
  MemorySettings,
  MemoryTopic,
  MemoryTopicDetail,
  MemoryWrite,
  Thread,
} from "./types";

const ROOT = ["memories"] as const;

export const memoryKeys = {
  all: ROOT,
  topics: [...ROOT, "topics"] as const,
  topic: (code: string) => [...ROOT, "topic", code] as const,
  entries: (q: EntryQuery) => [...ROOT, "entries", q] as const,
  entry: (number: number) => [...ROOT, "entry", number] as const,
  revisions: [...ROOT, "revisions"] as const,
  settings: [...ROOT, "settings"] as const,
  backfills: (unseen: boolean) => [...ROOT, "backfills", unseen] as const,
};

/** 来源标签 → 说法（§6.3：可信度从高到低） */
export const SOURCE_LABEL: Record<string, string> = {
  user: "用户亲自",
  agent: "Agent 写入",
  backfill: "会话后补记",
};

export const REVISION_KIND_LABEL: Record<string, string> = {
  write: "写入",
  undo: "撤销",
  restore: "恢复",
  move: "移动",
  split: "拆分",
};

/** 503 = 服务端没接入记忆服务：重试没有意义 */
function retry(n: number, err: Error): boolean {
  return !(err instanceof ApiError && err.status === 503) && n < 2;
}

export function isMemoryDisabled(err: unknown): boolean {
  return err instanceof ApiError && err.status === 503 && err.kind === "memory_not_enabled";
}

export function useMemoryTopics() {
  return useQuery({
    queryKey: memoryKeys.topics,
    queryFn: () => http.get<{ data: MemoryTopic[]; topic_max_chars: number }>("/v1/memories/topics"),
    retry,
  });
}

export function useMemoryTopic(code: string | null) {
  return useQuery({
    queryKey: memoryKeys.topic(code ?? ""),
    queryFn: () => http.get<MemoryTopicDetail>(`/v1/memories/topics/${encodeURIComponent(code!)}`),
    enabled: !!code,
    retry,
  });
}

export interface EntryQuery {
  status: "active" | "deleted" | "all";
  topic?: string;
  source?: string;
  q?: string;
  limit: number;
  offset: number;
}

export function useMemoryEntries(query: EntryQuery) {
  return useQuery({
    queryKey: memoryKeys.entries(query),
    queryFn: () =>
      http.get<MemoryEntryList>("/v1/memories/entries", {
        status: query.status,
        topic: query.topic,
        source: query.source,
        q: query.q,
        limit: query.limit,
        offset: query.offset,
      }),
    retry,
    placeholderData: (prev) => prev,
  });
}

export function useMemoryEntry(number: number | null) {
  return useQuery({
    queryKey: memoryKeys.entry(number ?? 0),
    queryFn: () => http.get<MemoryEntryDetail>(`/v1/memories/entries/${number}`),
    enabled: number !== null,
    retry,
  });
}

export function useMemoryRevisions(enabled: boolean) {
  return useQuery({
    queryKey: memoryKeys.revisions,
    queryFn: () => http.get<{ data: MemoryRevision[] }>("/v1/memories/revisions", { limit: 100 }),
    enabled,
    retry,
  });
}

export function useMemorySettings() {
  return useQuery({
    queryKey: memoryKeys.settings,
    queryFn: () => http.get<MemorySettings>("/v1/memories/settings"),
    retry,
  });
}

/** 会话后补记的结果（§4.3「上次会话补记了 N 条［查看］」）。unseen = 只要没看过的 */
export function useMemoryBackfills(unseen: boolean) {
  return useQuery({
    queryKey: memoryKeys.backfills(unseen),
    queryFn: () =>
      http.get<{ data: MemoryBackfill[] }>("/v1/memories/backfills", { unseen: unseen ? "true" : undefined }),
    retry: false,
    staleTime: 60_000,
  });
}

export const memoryApi = {
  addEntry: (body: { topic: string; content: string; topic_description?: string; resident?: boolean }) =>
    http.post<MemoryWrite>("/v1/memories/entries", body),
  correctEntry: (number: number, body: { content: string; topic?: string }) =>
    http.patch<MemoryWrite>(`/v1/memories/entries/${number}`, body),
  restoreEntry: (number: number, topic?: string) =>
    http.post<unknown>(`/v1/memories/entries/${number}/restore`, topic ? { topic } : {}),
  purgeEntry: (number: number) => http.del<{ purged: number[] }>(`/v1/memories/entries/${number}`),
  moveEntries: (entries: number[], toTopic: string) =>
    http.post<void>("/v1/memories/moves", { entries, to_topic: toTopic }),
  updateTopic: (code: string, body: { description?: string; resident?: boolean }) =>
    http.patch<MemoryTopic>(`/v1/memories/topics/${encodeURIComponent(code)}`, body),
  deleteTopic: (code: string) => http.del<void>(`/v1/memories/topics/${encodeURIComponent(code)}`),
  undo: (revisionId: string) => http.post<void>(`/v1/memories/revisions/${revisionId}/undo`),
  purgeAll: () => http.del<void>("/v1/memories"),
  setPaused: (paused: boolean) => http.put<MemorySettings>("/v1/memories/settings", { paused }),
  markBackfillsSeen: (ids?: string[]) =>
    http.post<{ updated: number }>("/v1/memories/backfills/seen", ids ? { ids } : {}),
};

/** 记忆的改动：成功后整体失效记忆相关的查询 */
export function useMemoryMutation<A, R>(fn: (arg: A) => Promise<R>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => qc.invalidateQueries({ queryKey: ROOT }),
  });
}

/**
 * 记忆的出处会话（条目 origin.thread）。★ 按 id 逐个取而不是用会话列表：列表只有最近 30 个，
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
