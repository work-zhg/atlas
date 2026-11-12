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
import { App as AntdApp, Button, Drawer, Empty, Skeleton } from "antd";
import { useEffect, useRef, useState } from "react";

import Link from "next/link";

import { downloadThreadFile, uploadThreadFile, useThreadFiles } from "@/api/threads";
import { workspaceHref } from "@/features/workspace/DirChip";
import { Icon } from "@/components/Icon";
import { compactNumber, relativeTime } from "@/lib/format";
import { BASE_PATH } from "@/lib/http";
import styles from "@atlas/chat-ui/src/chat/chat.module.css";

/**
 * 会话的文件：按需打开的抽屉（不再常驻一栏，对话区更宽）。
 * 文件数显示在对话头部的按钮上，所以这里的列表关着时也照常刷新。
 */
export function FilesDrawer({
  threadId,
  workspaceDir,
  running,
  writtenCount,
  open,
  onClose,
}: {
  threadId: string;
  /** 对话的工作目录（工作空间里的一个目录） */
  workspaceDir?: string;
  running: boolean;
  /** 本轮 file.written 的条数：变了就立刻刷新，不等定时器 */
  writtenCount: number;
  open: boolean;
  onClose: () => void;
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

  const pickRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const upload = async (picked: File[]) => {
    setUploading(true);
    try {
      for (const f of picked) await uploadThreadFile(threadId, f);
      message.success(`已上传 ${picked.length} 个文件`);
      void refetch();
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setUploading(false);
    }
  };

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={460}
      title="文件"
      extra={
        <Button
          size="small"
          type="text"
          aria-label="刷新文件列表"
          loading={filesQ.isFetching}
          icon={<Icon name="refresh" size={14} />}
          onClick={() => void refetch()}
        />
      }
      footer={
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 12, color: "var(--fg-3)", minWidth: 0 }}>
            工作目录{" "}
            {workspaceDir ? (
              <Link href={workspaceHref(workspaceDir)} title="在工作空间中打开">
                {workspaceDir}
              </Link>
            ) : (
              "—"
            )}
            ，智能体生成的文件都在这里
          </span>
          <Button
            style={{ marginLeft: "auto" }}
            icon={<Icon name="upload" size={14} />}
            loading={uploading}
            onClick={() => pickRef.current?.click()}
          >
            上传
          </Button>
          <input
            ref={pickRef}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              const picked = Array.from(e.target.files ?? []);
              e.target.value = "";
              if (picked.length) void upload(picked);
            }}
          />
        </div>
      }
    >
      <div>
        {filesQ.isPending && <Skeleton active paragraph={{ rows: 4 }} />}
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
                    href={`${BASE_PATH}/files/${threadId}?path=${encodeURIComponent(f.path)}`}
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
    </Drawer>
  );
}

/** 头部按钮上的文件数：和抽屉里的列表同一个查询，抽屉关着也能看到有几个 */
export function useFileCount(threadId: string, running: boolean): number {
  return useThreadFiles(threadId, running).data?.data.length ?? 0;
}
