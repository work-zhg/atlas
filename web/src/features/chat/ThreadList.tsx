"use client";

import { Button, Input, Skeleton, Tag } from "antd";
import { useMemo, useState } from "react";

import type { Thread } from "@/api/types";
import { Icon } from "@/components/Icon";
import { relativeTime } from "@/lib/format";
import { avatarBackground, avatarLetter } from "@/features/agents/avatar";
import styles from "./chat.module.css";
import { threadBadge } from "./threadState";

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

type Filter = "all" | "mine" | "active";

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
  const [filter, setFilter] = useState<Filter>("all");
  const [searching, setSearching] = useState(false);
  const [keyword, setKeyword] = useState("");

  const needsMe = threads.filter((t) => threadBadge(t)?.needsMe).length;

  // 筛选只作用于已加载的页：会话量级下够用，且不必为每个筛选再开一条后端查询
  const visible = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return threads.filter((t) => {
      const badge = threadBadge(t);
      if (filter === "mine" && !badge?.needsMe) return false;
      if (filter === "active" && !badge?.active) return false;
      return !kw || t.title.toLowerCase().includes(kw) || t.agent_name.toLowerCase().includes(kw);
    });
  }, [threads, filter, keyword]);
  const groups = useMemo(() => groupThreads(visible), [visible]);

  return (
    <aside className={`${styles.pane} ${styles.threads}`} aria-label="会话列表">
      <div className={styles.threadToolbar}>
        <Button type="primary" size="small" style={{ flex: 1 }} icon={<Icon name="plus" size={14} />} onClick={onNew}>
          新建会话
        </Button>
        <Button
          size="small"
          aria-label="搜索会话"
          aria-pressed={searching}
          icon={<Icon name="search" size={14} />}
          onClick={() => {
            setSearching((s) => !s);
            setKeyword("");
          }}
        />
      </div>
      {searching && (
        <div style={{ padding: "0 var(--s-4) var(--s-3)" }}>
          <Input
            size="small"
            autoFocus
            allowClear
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="搜索标题或智能体…"
          />
        </div>
      )}
      <div className={styles.threadFilters} role="tablist" aria-label="会话筛选">
        {(
          [
            ["all", "全部"],
            ["mine", "需要我处理"],
            ["active", "进行中"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={filter === key}
            className={styles.chip}
            onClick={() => setFilter(key)}
          >
            {label}
            {key === "mine" && needsMe > 0 && <b className={styles.chipCount}>{needsMe}</b>}
          </button>
        ))}
      </div>

      <div className={styles.paneBody}>
        {loading && <Skeleton active style={{ padding: "0 var(--s-4)" }} paragraph={{ rows: 5 }} />}

        {!loading && visible.length === 0 && (
          <p style={{ padding: "var(--s-5) var(--s-4)", color: "var(--fg-3)", fontSize: 12.5 }}>
            {threads.length === 0
              ? "还没有会话，点上方新建。"
              : filter === "mine"
                ? "没有需要你处理的会话。"
                : "没有匹配的会话。"}
          </p>
        )}

        {groups.map(([label, list]) => (
          <div key={label}>
            <div className={styles.groupLabel}>{label}</div>
            {list.map((t) => {
              const badge = threadBadge(t);
              return (
                <button
                  key={t.id}
                  className={styles.thread}
                  aria-current={t.id === activeId ? "true" : undefined}
                  onClick={() => onSelect(t.id)}
                >
                  <div className={styles.threadTop}>
                    <span className={styles.threadName} title={t.title}>
                      {t.title || "新会话"}
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
                    <span className={styles.threadAgent}>{t.agent_name}</span>
                    {badge ? (
                      <Tag color={badge.color} className={styles.threadBadge}>
                        {badge.live && <span className={styles.liveDot} aria-hidden="true" />}
                        {badge.label}
                      </Tag>
                    ) : (
                      <span>· {t.message_count} 条</span>
                    )}
                  </div>
                </button>
              );
            })}
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
