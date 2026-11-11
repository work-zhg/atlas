"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect } from "react";

import { ChatWorkbench } from "@/features/chat/ChatWorkbench";

/**
 * /chat/<threadId> → 打开该会话；/chat → 回首页（新对话从首页的输入框开始）。
 * 会话 URL 可直接分享 / 收藏，浏览器前进后退也符合直觉。
 */
export default function ChatPage() {
  const params = useParams<{ threadId?: string[] }>();
  const router = useRouter();
  const threadId = params.threadId?.[0];
  useEffect(() => {
    if (!threadId) router.replace("/");
  }, [threadId, router]);
  return threadId ? <ChatWorkbench threadId={threadId} /> : null;
}
