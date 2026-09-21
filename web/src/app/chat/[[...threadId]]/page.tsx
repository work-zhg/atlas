"use client";

import { useParams } from "next/navigation";

import { ChatWorkbench } from "@/features/chat/ChatWorkbench";

/**
 * /chat            → 未选择会话
 * /chat/<threadId> → 打开该会话
 *
 * 用可选 catch-all 而不是 query string：会话 URL 可直接分享/收藏，
 * 浏览器前进后退也符合直觉。
 */
export default function ChatPage() {
  const params = useParams<{ threadId?: string[] }>();
  const threadId = params.threadId?.[0];
  return <ChatWorkbench threadId={threadId} />;
}
