"use client";

/**
 * 会话右侧：只看文件（会话工作区里实际有的东西）。
 *
 * ★ 数据直接来自对象存储（GET /v1/threads/{id}/files），不再由 file.written
 *   事件拼：acp 子智能体的 CLI 在 Pod 里直接写工作区，平台侧没有事件 ——
 *   此前那类产物在这里永远是空的。
 * ★ 点文件名在新标签打开预览页（/files/{threadId}?path=…）。
 * ★ 按目录层级展示，目录默认收起。接口只列文件，目录是从路径推出来的 —— 空目录不显示。
 * ★ 不放计划 / 工具 / token：计划与工具调用已经按发生顺序出现在对话里，
 *   这里只回答「这个会话产出了什么、怎么拿走」。
 */
import { App as AntdApp, Button, Drawer, Empty, Skeleton } from "antd";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import Link from "next/link";

import { downloadThreadFile, uploadThreadFile, useThreadFiles } from "@/api/threads";
import type { WorkspaceFilesOut } from "@/api/types";
import { workspaceHref } from "@/features/workspace/DirChip";
import { Icon } from "@/components/Icon";
import { compactNumber, relativeTime } from "@/lib/format";
import { BASE_PATH } from "@/lib/http";
import styles from "@atlas/chat-ui/src/chat/chat.module.css";

type FileItem = WorkspaceFilesOut["data"][number];

/** 目录树的一层 */
interface DirNode {
  name: string;
  path: string;
  dirs: Map<string, DirNode>;
  files: FileItem[];
  /** 下面（含子目录）一共几个文件 */
  count: number;
}

function buildTree(files: FileItem[]): DirNode {
  const root: DirNode = { name: "", path: "", dirs: new Map(), files: [], count: 0 };
  for (const f of files) {
    let node = root;
    node.count += 1;
    for (const part of f.path.split("/").slice(0, -1)) {
      let child = node.dirs.get(part);
      if (!child) {
        const path = node.path ? `${node.path}/${part}` : part;
        child = { name: part, path, dirs: new Map(), files: [], count: 0 };
        node.dirs.set(part, child);
      }
      child.count += 1;
      node = child;
    }
    node.files.push(f);
  }
  return root;
}

const byName = (a: string, b: string) => a.localeCompare(b, "zh-CN", { numeric: true });
const baseName = (path: string) => path.slice(path.lastIndexOf("/") + 1);

/** 每深一层缩进 = 展开箭头 + 间距：子级的箭头正好对着父级的文件夹图标 */
const STEP = 19;
const indent = (depth: number) => 7 + depth * STEP;

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

  const files = filesQ.data?.data;
  const tree = useMemo(() => buildTree(files ?? []), [files]);
  // ★ 展开状态按目录路径记：列表定时刷新时不会被收起
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const toggle = (path: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (!next.delete(path)) next.add(path);
      return next;
    });

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

  const renderFile = (f: FileItem, depth: number) => (
    <div
      key={f.path}
      className={styles.fileRow}
      style={{ paddingLeft: indent(depth) + STEP }}
      title={f.path}
    >
      <Icon name="file" size={14} style={{ color: "var(--fg-3)", flex: "none" }} />
      {/* ★ 新标签打开预览页：对话还在原标签，边看产物边说话 */}
      <a
        className={`${styles.fileName} ${styles.fileLink}`}
        href={`${BASE_PATH}/files/${threadId}?path=${encodeURIComponent(f.path)}`}
        target="_blank"
        rel="noopener noreferrer"
        title={`在新标签预览 ${f.path}`}
      >
        {baseName(f.path)}
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

  /** 一层：目录在前、文件在后，展开的目录接着列它下面的 */
  const renderDir = (node: DirNode, depth: number): ReactNode[] => [
    ...[...node.dirs.values()]
      .sort((a, b) => byName(a.name, b.name))
      .flatMap((d) => {
        const open = expanded.has(d.path);
        return [
          <button
            key={`dir:${d.path}`}
            type="button"
            className={styles.fileRow}
            style={{
              paddingLeft: indent(depth),
              minHeight: 34, // 和文件行（带下载按钮）一样高，行距才齐
              border: 0,
              background: "none",
              color: "inherit",
              fontFamily: "inherit",
              cursor: "pointer",
            }}
            aria-expanded={open}
            title={d.path}
            onClick={() => toggle(d.path)}
          >
            <Icon name={open ? "down" : "right"} size={12} style={{ color: "var(--fg-3)", flex: "none" }} />
            <Icon name="folder" size={14} style={{ color: "var(--fg-3)", flex: "none" }} />
            <span className={styles.fileName}>{d.name}</span>
            <span className={styles.fileSize}>{d.count} 个文件</span>
          </button>,
          ...(open ? renderDir(d, depth + 1) : []),
        ];
      }),
    ...[...node.files].sort((a, b) => byName(baseName(a.path), baseName(b.path))).map((f) => renderFile(f, depth)),
  ];

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={460}
      title="文件"
      extra={
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          {workspaceDir && (
            <Link
              href={workspaceHref(workspaceDir)}
              title={`在工作空间中打开 ${workspaceDir}`}
              style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 12.5 }}
            >
              <Icon name="folder" size={14} />
              在工作空间中打开
            </Link>
          )}
          <Button
            size="small"
            type="text"
            aria-label="刷新文件列表"
            loading={filesQ.isFetching}
            icon={<Icon name="refresh" size={14} />}
            onClick={() => void refetch()}
          />
        </span>
      }
      footer={
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 12, color: "var(--fg-3)", minWidth: 0 }}>
            工作目录 <span style={{ fontFamily: "var(--mono)" }}>{workspaceDir || "—"}</span>
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
        {filesQ.data?.configured && tree.count === 0 && (
          <Empty description="工作区还没有文件" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        )}
        {tree.count > 0 && (
          <>
            {renderDir(tree, 0)}
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
