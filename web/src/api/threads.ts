"use client";

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { apiUrl, authHeaders, http } from "@/lib/http";
import { qk } from "./keys";
import type {
  Message,
  Page,
  PreviewSessionOut,
  Thread,
  ThreadCreate,
  WorkspaceFilesOut,
} from "./types";

/** 会话列表 —— keyset 游标分页（文档 §11，不用 OFFSET）。 */
export function useThreads(status?: string) {
  return useInfiniteQuery({
    queryKey: qk.threads.list(status),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      http.get<Page<Thread>>("/v1/threads", { status, cursor: pageParam, limit: 30 }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    // 列表上有「运行中 / 待审批」的会话时定时刷新徽标；全部安静时不轮询
    refetchInterval: (query) =>
      query.state.data?.pages.some((p) =>
        p.data.some((t) => t.last_run && UNFINISHED.has(t.last_run.status)),
      )
        ? 10_000
        : false,
  });
}

const UNFINISHED = new Set(["queued", "running", "awaiting_approval", "suspended"]);

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

/**
 * 会话工作区里的文件（右侧「文件」面板）—— 直接列对象存储。
 *
 * ★ 不靠 file.written 事件：acp 子智能体的 CLI 在 Pod 里直接写工作区，平台侧
 *   没有事件。运行中定时刷新，正是为了看见这类产物。
 */
export function useThreadFiles(threadId: string | undefined, running: boolean) {
  return useQuery({
    queryKey: ["threads", "files", threadId ?? ""],
    queryFn: () => http.get<WorkspaceFilesOut>(`/v1/threads/${threadId}/files`),
    enabled: Boolean(threadId),
    refetchInterval: running ? 10_000 : false,
  });
}

/**
 * 文件预览站点的入口（doc/detail/file-preview.html §06）。
 *
 * ★ 令牌在 URL 路径里：iframe / img 带不了 X-User-Id。后端空闲 1 小时才失效、
 *   每次访问都续期，所以这里 50 分钟内复用同一个；失效（410）时调用方 refetch。
 */
export function usePreviewSession(threadId: string | undefined) {
  return useQuery({
    queryKey: ["threads", "preview", threadId ?? ""],
    queryFn: () => http.post<PreviewSessionOut>(`/v1/threads/${threadId}/files/preview`),
    enabled: Boolean(threadId),
    staleTime: 50 * 60_000,
    refetchOnWindowFocus: false,
  });
}

/** 站点里某个文件的地址。★ 逐段编码：中文、空格、#、? 都要转义，/ 不能转。 */
export function previewFileUrl(baseUrl: string, path: string): string {
  return baseUrl + path.split("/").map(encodeURIComponent).join("/");
}

export class PreviewFetchError extends Error {
  constructor(readonly status: number) {
    super(status === 410 ? "预览已过期" : status === 404 ? "文件已不存在" : `读取失败（${status}）`);
  }
}

export interface PreviewText {
  /** 只按 UTF-8 解码；截断点落在多字节字符中间时末尾的替换符已去掉 */
  text: string;
  /** 前 maxBytes 字节是否是全部内容 */
  truncated: boolean;
  /** 文件总字节数 */
  total: number;
  /** 前 8 KiB 里有 NUL —— 当作二进制，不显示 */
  binary: boolean;
}

/**
 * 读取文件开头的 maxBytes 字节（Range）。
 *
 * ★ 小文件 S3 返回 200 而不是 206，两种都接受。总大小从 Content-Range 取。
 */
export async function fetchPreviewText(url: string, maxBytes: number): Promise<PreviewText> {
  const res = await fetch(url, { headers: { Range: `bytes=0-${maxBytes - 1}` } });
  if (!res.ok) throw new PreviewFetchError(res.status);
  const bytes = new Uint8Array(await res.arrayBuffer());
  const range = res.headers.get("Content-Range");
  const total = range ? Number(range.split("/")[1]) : bytes.length;
  const binary = bytes.subarray(0, 8192).includes(0);
  const truncated = total > bytes.length;
  let text = binary ? "" : new TextDecoder("utf-8").decode(bytes);
  if (truncated && text.endsWith("\uFFFD")) text = text.slice(0, -1);
  return { text, truncated, total: Number.isFinite(total) ? total : bytes.length, binary };
}

/** 下载工作区文件。★ 走 fetch 而不是 <a href>：要带身份头（X-User-Id）。 */
export async function downloadThreadFile(threadId: string, path: string): Promise<void> {
  const res = await fetch(apiUrl(`/v1/threads/${threadId}/files/download`, { path }), {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(`下载失败（${res.status}）`);
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = path.split("/").pop() || "file";
  a.click();
  URL.revokeObjectURL(url);
}
