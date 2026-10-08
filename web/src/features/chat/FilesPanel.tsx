"use client";

/**
 * 会话右侧：只看文件（会话工作区里实际有的东西）。
 *
 * ★ 数据直接来自对象存储（GET /v1/threads/{id}/files），不再由 file.written
 *   事件拼：acp 子智能体的 CLI 在 Pod 里直接写工作区，平台侧没有事件 ——
 *   此前那类产物在这里永远是空的。
 * ★ 点文件名在新标签打开预览页（/files/{threadId}?path=…）。
 * ★ 不放计划 / 工具 / token：计划与工具调用已经按发生顺序出现在对话里，
 *   这里只回答「这个会话产出了什么、怎么拿走」。
 */
import { App as AntdApp, Button, Empty, Skeleton } from "antd";
import { useEffect, useState } from "react";

import { downloadThreadFile, useThreadFiles } from "@/api/threads";
import { Icon } from "@/components/Icon";
import { compactNumber, relativeTime } from "@/lib/format";
import styles from "./chat.module.css";

export function FilesPanel({
  threadId,
  running,
  writtenCount,
}: {
  threadId?: string;
  running: boolean;
  /** 本轮 file.written 的条数：变了就立刻刷新，不等定时器 */
  writtenCount: number;
}) {
  const { message } = AntdApp.useApp();
  const filesQ = useThreadFiles(threadId, running);
  const [busy, setBusy] = useState<string>();
  const { refetch } = filesQ;

  useEffect(() => {
    if (threadId) void refetch();
    // 一轮结束时也刷新一次：最后一批产物往往在收尾时才写完
  }, [threadId, writtenCount, running, refetch]);

  const files = filesQ.data?.data ?? [];

  const download = async (path: string) => {
    if (!threadId) return;
    setBusy(path);
    try {
      await downloadThreadFile(threadId, path);
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBusy(undefined);
    }
  };

  return (
    <aside className={`${styles.pane} ${styles.inspector}`} aria-label="会话文件">
      <div className={styles.paneHead} style={{ justifyContent: "space-between" }}>
        <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 600 }}>
          <Icon name="folder" size={14} />
          文件
          {files.length > 0 && <span className={styles.toolCost}>{files.length}</span>}
        </span>
        {threadId && (
          <Button
            size="small"
            type="text"
            aria-label="刷新文件列表"
            loading={filesQ.isFetching}
            icon={<Icon name="retry" size={13} />}
            onClick={() => void refetch()}
          />
        )}
      </div>

      <div className={styles.paneBody} style={{ padding: "var(--s-3)" }}>
        {!threadId && <Empty description="选择会话后显示它的文件" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
        {threadId && filesQ.isPending && <Skeleton active paragraph={{ rows: 4 }} />}
        {filesQ.error && (
          <p style={{ color: "var(--danger)", fontSize: 12.5 }}>加载失败：{filesQ.error.message}</p>
        )}
        {filesQ.data && !filesQ.data.configured && (
          <Empty
            description="这个部署没有配置对象存储，会话没有文件能力"
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          />
        )}
        {filesQ.data?.configured && files.length === 0 && (
          <Empty description="工作区还没有文件" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        )}
        {files.length > 0 && (
          <>
            <p className={styles.insSection}>工作区 · 与子智能体共享</p>
            {files.map((f) => {
              const slash = f.path.lastIndexOf("/");
              const dir = slash >= 0 ? f.path.slice(0, slash + 1) : "";
              const name = f.path.slice(slash + 1);
              return (
                <div key={f.path} className={styles.fileRow} title={f.path}>
                  <Icon name="file" size={14} style={{ color: "var(--fg-3)", flex: "none" }} />
                  {/* ★ 新标签打开预览页：对话还在原标签，边看产物边说话 */}
                  <a
                    className={`${styles.fileName} ${styles.fileLink}`}
                    href={`/files/${threadId}?path=${encodeURIComponent(f.path)}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={`在新标签预览 ${f.path}`}
                  >
                    {dir && <span style={{ color: "var(--fg-3)" }}>{dir}</span>}
                    {name}
                  </a>
                  <span className={styles.fileSize} title={f.modified_at ?? undefined}>
                    {compactNumber(f.size)}B · {relativeTime(f.modified_at)}
                  </span>
                  <Button
                    size="small"
                    type="text"
                    aria-label={`下载 ${f.path}`}
                    loading={busy === f.path}
                    icon={<Icon name="download" size={13} />}
                    onClick={() => void download(f.path)}
                  />
                </div>
              );
            })}
            {filesQ.data?.truncated && (
              <p style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 8 }}>文件太多，只列出了一部分。</p>
            )}
          </>
        )}
      </div>
    </aside>
  );
}
