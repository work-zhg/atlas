"use client";

/** 对话需要的会话读取 hooks（会话详情、消息分页）。会话列表 / 新建 / 文件等留在 Atlas 前端。 */
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { useAtlasHttp } from "../client";
import { qk } from "./keys";
import type { Message, Page, Thread } from "./types";

export function useThread(threadId: string | undefined) {
  const http = useAtlasHttp();
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
  const http = useAtlasHttp();
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
