"use client";

/**
 * 智能平台左侧的「最近对话」（同 ChatGPT / Claude）：取代原来单独的一栏会话列表，对话区更宽。
 *
 * ★ 进行中 / 等你确认的会话标一个圆点：用户离开后要能一眼找回需要处理的那条。
 * ★ 搜索只在已加载的会话里按标题和智能体名找：会话量级下够用，不必为它再开一条后端查询。
 */
import { App as AntdApp, Dropdown, Input, Modal } from "antd";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { useDeleteThread, useRenameThread, useThreads } from "@/api/threads";
import type { Thread } from "@/api/types";
import { Icon } from "@/components/Icon";
import styles from "@/components/shell/shell.module.css";
import { appName, threadBadge } from "./threadState";

const DAY = 86_400_000;

function groupThreads(threads: Thread[]): [string, Thread[]][] {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const buckets: Record<string, Thread[]> = { 今天: [], 昨天: [], "近 7 天": [], 更早: [] };
  for (const t of threads) {
    const ts = new Date(t.updated_at).getTime();
    const key = ts >= today ? "今天" : ts >= today - DAY ? "昨天" : ts >= today - 6 * DAY ? "近 7 天" : "更早";
    buckets[key]!.push(t);
  }
  return Object.entries(buckets).filter(([, list]) => list.length > 0);
}

function RenameModal({ thread, onClose }: { thread: Thread; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const rename = useRenameThread(thread.id);
  const [title, setTitle] = useState(thread.title);
  return (
    <Modal
      open
      title="重命名对话"
      okText="保存"
      cancelText="取消"
      confirmLoading={rename.isPending}
      onCancel={onClose}
      onOk={() =>
        rename.mutate(title.trim() || thread.title, {
          onSuccess: onClose,
          onError: (e) => message.error(`没有保存成功：${e.message}`),
        })
      }
    >
      <Input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} autoFocus />
    </Modal>
  );
}

export function ThreadHistory() {
  const router = useRouter();
  const pathname = usePathname();
  const { message, modal } = AntdApp.useApp();
  const threadsQ = useThreads();
  const del = useDeleteThread();
  const [searching, setSearching] = useState(false);
  const [keyword, setKeyword] = useState("");
  const [renaming, setRenaming] = useState<Thread>();

  const threads = useMemo(() => threadsQ.data?.pages.flatMap((p) => p.data) ?? [], [threadsQ.data]);
  const visible = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return kw
      ? threads.filter((t) => t.title.toLowerCase().includes(kw) || t.agent_name.toLowerCase().includes(kw))
      : threads;
  }, [threads, keyword]);
  const groups = useMemo(() => groupThreads(visible), [visible]);

  const remove = (t: Thread) =>
    modal.confirm({
      title: "删除这个对话？",
      content: "对话记录和它的文件会一起删除，不能恢复。",
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: () =>
        del.mutateAsync(t.id).then(
          () => {
            if (pathname === `/chat/${t.id}`) router.push("/");
          },
          (e: Error) => {
            message.error(`没有删除成功：${e.message}`);
          },
        ),
    });

  return (
    <div className={styles.hist}>
      <div className={styles.histHead}>
        <span>最近对话</span>
        <button
          type="button"
          className={styles.iconBtn}
          title="搜索对话"
          aria-label="搜索对话"
          onClick={() => {
            setSearching((v) => !v);
            setKeyword("");
          }}
        >
          <Icon name={searching ? "x" : "search"} size={14} />
        </button>
      </div>
      {searching && (
        <Input
          size="small"
          allowClear
          autoFocus
          placeholder="按标题或智能体找"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          style={{ margin: "2px 0 6px" }}
        />
      )}

      {threadsQ.isPending && <div className={styles.histGroup}>加载中…</div>}
      {!threadsQ.isPending && visible.length === 0 && (
        <div className={styles.histGroup}>{keyword ? "没有找到" : "还没有对话"}</div>
      )}

      {groups.map(([label, list]) => (
        <div key={label}>
          <div className={styles.histGroup}>{label}</div>
          {list.map((t) => {
            const badge = threadBadge(t);
            const active = pathname === `/chat/${t.id}`;
            return (
              <Link
                key={t.id}
                href={`/chat/${t.id}`}
                className={styles.histItem}
                aria-current={active ? "page" : undefined}
                title={`${t.title} · ${t.agent_name}`}
              >
                {badge?.needsMe ? (
                  <span className={`${styles.dot} ${styles.dotWait}`} title="等你确认" />
                ) : badge?.live ? (
                  <span className={`${styles.dot} ${styles.dotRun}`} title="进行中" />
                ) : null}
                <span className={styles.histTitle}>{t.title || "新对话"}</span>
                {t.source_app && <span className={styles.histTag}>{appName(t.source_app)}</span>}
                <Dropdown
                  trigger={["click"]}
                  menu={{
                    items: [
                      { key: "rename", icon: <Icon name="pencil" size={14} />, label: "重命名" },
                      { type: "divider" },
                      { key: "delete", icon: <Icon name="trash" size={14} />, label: "删除", danger: true },
                    ],
                    onClick: ({ key, domEvent }) => {
                      domEvent.preventDefault();
                      domEvent.stopPropagation();
                      if (key === "rename") setRenaming(t);
                      if (key === "delete") remove(t);
                    },
                  }}
                >
                  <button
                    type="button"
                    className={`${styles.iconBtn} ${styles.histMore}`}
                    aria-label="更多"
                    onClick={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                    }}
                  >
                    <Icon name="more" size={14} />
                  </button>
                </Dropdown>
              </Link>
            );
          })}
        </div>
      ))}

      {threadsQ.hasNextPage && !keyword && (
        <button
          type="button"
          className={styles.item}
          style={{ justifyContent: "center", color: "var(--fg-3)", fontSize: 13 }}
          onClick={() => void threadsQ.fetchNextPage()}
          disabled={threadsQ.isFetchingNextPage}
        >
          {threadsQ.isFetchingNextPage ? "加载中…" : "加载更多"}
        </button>
      )}

      {renaming && <RenameModal thread={renaming} onClose={() => setRenaming(undefined)} />}
    </div>
  );
}
