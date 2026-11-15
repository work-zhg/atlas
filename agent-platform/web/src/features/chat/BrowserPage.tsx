"use client";

import { useEffect } from "react";

import { useThread } from "@/api/threads";
import { Icon } from "@/components/Icon";
import { BrowserView } from "@atlas/chat-ui/src/chat/BrowserView";

import styles from "./browser.module.css";

/**
 * 会话浏览器的独立页（/browser/<threadId>）：从对话头部的「浏览器」在新标签打开，
 * 一边在对话里看智能体说什么、一边在这里看它在浏览器里做什么。
 *
 * ★ 只看不操作，免得打乱智能体；要你登录、输验证码时，对话里会出现接管卡片。
 */
export function BrowserPage({ threadId }: { threadId: string }) {
  const threadQ = useThread(threadId);
  const title = threadQ.data?.title || "对话";
  useEffect(() => {
    document.title = `${title} · 浏览器`;
  }, [title]);

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <Icon name="globe" size={16} style={{ color: "var(--fg-3)", flex: "none" }} />
        <span className={styles.title}>{title}</span>
        <span className={styles.meta}>只看不操作；要你登录时，对话里会出现接管卡片</span>
      </header>
      <div className={styles.body}>
        {/* 画面自带上边距：高度扣掉它 */}
        <BrowserView threadId={threadId} interactive={false} height="calc(100% - var(--s-3))" />
      </div>
    </div>
  );
}
