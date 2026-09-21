"use client";

import { Button, Skeleton } from "antd";
import { useMemo } from "react";

import type { Thread } from "@/api/types";
import { Icon } from "@/components/Icon";
import { relativeTime } from "@/lib/format";
import { avatarBackground, avatarLetter } from "@/features/agents/avatar";
import styles from "./chat.module.css";

interface Props {
  threads: Thread[];
  activeId?: string;
  loading: boolean;
  hasMore: boolean;
  loadingMore: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onLoadMore: () => void;
}

const DAY = 86_400_000;

/** 按时间分组 —— 原型的「今天 / 最近 7 天 / 更早」。 */
function groupThreads(threads: Thread[]): [string, Thread[]][] {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();

  const buckets: Record<string, Thread[]> = { 今天: [], "最近 7 天": [], 更早: [] };
  for (const t of threads) {
    const ts = new Date(t.updated_at).getTime();
    const key = ts >= startOfToday ? "今天" : ts >= startOfToday - 6 * DAY ? "最近 7 天" : "更早";
    buckets[key]!.push(t);
  }
  return Object.entries(buckets).filter(([, list]) => list.length > 0);
}

export function ThreadList({
  threads,
  activeId,
  loading,
  hasMore,
  loadingMore,
  onSelect,
  onNew,
  onLoadMore,
}: Props) {
  const groups = useMemo(() => groupThreads(threads), [threads]);

  return (
    <aside className={`${styles.pane} ${styles.threads}`} aria-label="会话列表">
      <div className={styles.threadToolbar}>
        <Button type="primary" size="small" style={{ flex: 1 }} icon={<Icon name="plus" size={14} />} onClick={onNew}>
          新建会话
        </Button>
      </div>

      <div className={styles.paneBody}>
        {loading && <Skeleton active style={{ padding: "0 var(--s-4)" }} paragraph={{ rows: 5 }} />}

        {!loading && threads.length === 0 && (
          <p style={{ padding: "var(--s-5) var(--s-4)", color: "var(--fg-3)", fontSize: 12.5 }}>
            还没有会话，点上方新建。
          </p>
        )}

        {groups.map(([label, list]) => (
          <div key={label}>
            <div className={styles.groupLabel}>{label}</div>
            {list.map((t) => (
              <button
                key={t.id}
                className={styles.thread}
                aria-current={t.id === activeId ? "true" : undefined}
                onClick={() => onSelect(t.id)}
              >
                <div className={styles.threadTop}>
                  <span className={styles.threadName} title={t.title}>
                    {t.title}
                  </span>
                  <span className={styles.threadTime}>{relativeTime(t.updated_at)}</span>
                </div>
                <div className={styles.threadSub}>
                  <span
                    className={styles.miniAvatar}
                    style={{ background: avatarBackground(t.agent_avatar_key) }}
                    aria-hidden="true"
                  >
                    {avatarLetter(t.agent_name)}
                  </span>
                  {t.agent_name} · {t.message_count} 条
                </div>
              </button>
            ))}
          </div>
        ))}

        {hasMore && (
          <div style={{ padding: "var(--s-3) var(--s-4)" }}>
            <Button size="small" block loading={loadingMore} onClick={onLoadMore}>
              加载更早
            </Button>
          </div>
        )}
      </div>
    </aside>
  );
}
