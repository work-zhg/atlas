"use client";

import { useParams } from "next/navigation";

import { BrowserPage } from "@/features/chat/BrowserPage";

/** /browser/<threadId> —— 会话浏览器的实时画面，从对话头部的「浏览器」新标签打开 */
export default function BrowserRoute() {
  const { threadId } = useParams<{ threadId: string }>();
  return <BrowserPage threadId={threadId} />;
}
