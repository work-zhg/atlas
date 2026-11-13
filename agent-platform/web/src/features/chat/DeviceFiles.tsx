"use client";

/**
 * 本机 CLI 会话的文件（agent-platform/doc/design/device-cli-design.html §5）：项目目录在使用者自己的
 * 电脑上，打开时经设备网关现读，不同步到平台。只读：能看、能下载，不能上传、删除。
 *
 *   改动   git 仓库里还没提交的改动（新增、修改、删除），git 仓库时默认打开；一轮进行中定时刷新。
 *          是工作区相对最近一次提交的改动，包括用户自己改的，不精确对应这个会话做了什么
 *   全部   按目录一层层加载（展开时才读），一层太多时只给前面一部分
 *
 * ★ 电脑不在线时显示原因（不说「系统忙」）；电脑重新连上后自动恢复。
 */
import { App as AntdApp, Button, Drawer, Empty, Segmented, Skeleton, Tag } from "antd";
import { type ReactNode, useEffect, useState } from "react";

import { type ThreadDevice, useDeviceChanges, useDeviceDir } from "@/api/devices";
import { downloadThreadFile } from "@/api/threads";
import { Icon } from "@/components/Icon";
import { compactNumber, relativeTime } from "@/lib/format";
import { BASE_PATH } from "@/lib/http";
import styles from "@atlas/chat-ui/src/chat/chat.module.css";

type Tab = "changes" | "all";

const STATUS: Record<string, [string, string]> = {
  A: ["新增", "green"],
  M: ["修改", "blue"],
  D: ["删除", "red"],
  R: ["改名", "purple"],
};

const STEP = 19;
const indent = (depth: number) => 7 + depth * STEP;
const join = (dir: string, name: string) => (dir ? `${dir}/${name}` : name);
const fromEpoch = (s?: number) => (s ? new Date(s * 1000).toISOString() : null);

export function DeviceFilesDrawer({
  threadId,
  device,
  running,
  open,
  onClose,
}: {
  threadId: string;
  device: ThreadDevice;
  running: boolean;
  open: boolean;
  onClose: () => void;
}) {
  const { message } = AntdApp.useApp();
  const changesQ = useDeviceChanges(threadId, true, running);
  const isRepo = changesQ.data?.is_repo ?? false;
  const [tab, setTab] = useState<Tab>("changes");
  // 不是 git 仓库只有「全部」
  useEffect(() => {
    if (changesQ.isSuccess && !isRepo) setTab("all");
  }, [changesQ.isSuccess, isRepo]);
  const [busy, setBusy] = useState<string>();
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const toggle = (path: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (!next.delete(path)) next.add(path);
      return next;
    });

  const download = async (path: string) => {
    setBusy(path);
    try {
      await downloadThreadFile(threadId, path);
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBusy(undefined);
    }
  };

  const fileRow = (path: string, label: ReactNode, meta: ReactNode, depth: number, canOpen = true) => (
    <div key={path} className={styles.fileRow} style={{ paddingLeft: indent(depth) + STEP }} title={path}>
      <Icon name="file" size={14} style={{ color: "var(--fg-3)", flex: "none" }} />
      {canOpen ? (
        <a
          className={`${styles.fileName} ${styles.fileLink}`}
          href={`${BASE_PATH}/files/${threadId}?path=${encodeURIComponent(path)}`}
          target="_blank"
          rel="noopener noreferrer"
          title={`在新标签预览 ${path}`}
        >
          {label}
        </a>
      ) : (
        <span className={styles.fileName} style={{ textDecoration: "line-through", color: "var(--fg-3)" }}>
          {label}
        </span>
      )}
      <span className={styles.fileSize}>{meta}</span>
      {canOpen && (
        <Button
          size="small"
          type="text"
          aria-label={`下载 ${path}`}
          loading={busy === path}
          icon={<Icon name="download" size={13} />}
          onClick={() => void download(path)}
        />
      )}
    </div>
  );

  const offline = device.status === "offline" || changesQ.error;

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
          aria-label="刷新"
          loading={changesQ.isFetching}
          icon={<Icon name="refresh" size={14} />}
          onClick={() => void changesQ.refetch()}
        />
      }
      footer={
        <span style={{ fontSize: 12, color: "var(--fg-3)" }}>
          <Icon name="laptop" size={13} style={{ verticalAlign: -2 }} /> {device.name}{" "}
          <span style={{ fontFamily: "var(--mono)" }}>{device.project_dir}</span>
          ：文件在你的电脑上，打开时现读，只读
        </span>
      }
    >
      {offline ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            changesQ.error?.message ??
            `你的电脑「${device.name}」不在线，文件暂时看不到；启动连接器后自动恢复`
          }
        />
      ) : (
        <>
          {isRepo && (
            <Segmented<Tab>
              block
              style={{ marginBottom: 12 }}
              value={tab}
              onChange={setTab}
              options={[
                { value: "changes", label: `改动${changesQ.data ? ` ${changesQ.data.entries.length}` : ""}` },
                { value: "all", label: "全部" },
              ]}
            />
          )}
          {changesQ.isPending && <Skeleton active paragraph={{ rows: 4 }} />}
          {tab === "changes" && changesQ.data && (
            <>
              <p style={{ fontSize: 12, color: "var(--fg-3)", margin: "0 0 8px" }}>
                还没提交的改动，包括你自己改的，不一定都是这个对话做的
              </p>
              {changesQ.data.entries.length === 0 ? (
                <Empty description="没有还没提交的改动" image={Empty.PRESENTED_IMAGE_SIMPLE} />
              ) : (
                changesQ.data.entries.map((e) => {
                  const [label, color] = STATUS[e.status] ?? [e.status, "default"];
                  return fileRow(
                    e.path,
                    e.path,
                    <>
                      <Tag color={color} style={{ marginInlineEnd: 4 }}>
                        {label}
                      </Tag>
                      {e.size !== undefined && `${compactNumber(e.size)}B`}
                    </>,
                    -1,
                    e.status !== "D", // 删除的只列出，不能预览
                  );
                })
              )}
            </>
          )}
          {tab === "all" && (
            <DirLevel
              threadId={threadId}
              dir=""
              depth={0}
              expanded={expanded}
              onToggle={toggle}
              renderFile={(path, name, meta, depth) => fileRow(path, name, meta, depth)}
            />
          )}
        </>
      )}
    </Drawer>
  );
}

/** 一层目录：展开时才读 */
function DirLevel({
  threadId,
  dir,
  depth,
  expanded,
  onToggle,
  renderFile,
}: {
  threadId: string;
  dir: string;
  depth: number;
  expanded: ReadonlySet<string>;
  onToggle: (path: string) => void;
  renderFile: (path: string, name: string, meta: ReactNode, depth: number) => ReactNode;
}) {
  const dirQ = useDeviceDir(threadId, dir, true);
  if (dirQ.isPending) return <Skeleton active title={false} paragraph={{ rows: 2 }} style={{ paddingLeft: indent(depth) }} />;
  if (dirQ.error)
    return <p style={{ color: "var(--danger)", fontSize: 12.5, paddingLeft: indent(depth) }}>{dirQ.error.message}</p>;
  const entries = dirQ.data.entries;
  if (depth === 0 && entries.length === 0)
    return <Empty description="项目目录是空的" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  return (
    <>
      {entries.map((e) => {
        const path = join(dir, e.name);
        if (!e.is_dir)
          return renderFile(path, e.name, `${compactNumber(e.size ?? 0)}B · ${relativeTime(fromEpoch(e.modified_at))}`, depth);
        const open = expanded.has(path);
        return (
          <div key={path}>
            <button
              type="button"
              className={styles.fileRow}
              style={{
                paddingLeft: indent(depth),
                minHeight: 34,
                border: 0,
                background: "none",
                color: "inherit",
                fontFamily: "inherit",
                cursor: "pointer",
                width: "100%",
              }}
              aria-expanded={open}
              title={path}
              onClick={() => onToggle(path)}
            >
              <Icon name={open ? "down" : "right"} size={12} style={{ color: "var(--fg-3)", flex: "none" }} />
              <Icon name="folder" size={14} style={{ color: "var(--fg-3)", flex: "none" }} />
              <span className={styles.fileName}>{e.name}</span>
            </button>
            {open && (
              <DirLevel
                threadId={threadId}
                dir={path}
                depth={depth + 1}
                expanded={expanded}
                onToggle={onToggle}
                renderFile={renderFile}
              />
            )}
          </div>
        );
      })}
      {dirQ.data.truncated && (
        <p style={{ fontSize: 12, color: "var(--fg-3)", paddingLeft: indent(depth) + STEP }}>
          这一层太多，只列出了前面一部分；要找的文件请到电脑上打开。
        </p>
      )}
    </>
  );
}
