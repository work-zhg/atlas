"use client";

/**
 * 工作空间右侧的预览：列表留在左边，↑ ↓ 切换文件，Esc 关闭。
 *
 * ★ 渲染沿用对话里的预览页（features/files/renderers）：内容经预览站点读取，令牌的根是整个
 *   工作空间（POST /v1/workspace/preview），所以 HTML 引用的同目录 CSS、JS、图片照样能加载。
 */
import { App as AntdApp, Alert, Button, Segmented, Tooltip } from "antd";
import { useEffect, useRef, useState } from "react";

import { PreviewFetchError, previewFileUrl } from "@/api/threads";
import { downloadWorkspaceFile, useWorkspacePreview, type WorkspaceFile } from "@/api/workspace";
import { Icon } from "@/components/Icon";
import { formatBytes } from "@/features/files/format";
import { extOf, IMAGE_MAX_BYTES, previewKind } from "@/features/files/previewKind";
import {
  HtmlFrame,
  ImageView,
  Loading,
  MarkdownView,
  Message,
  PdfFrame,
  TextView,
  usePreviewText,
} from "@/features/files/renderers";
import { relativeTime } from "@/lib/format";

import styles from "./workspace.module.css";

type View = "preview" | "source";

export function WorkspacePreview({
  file,
  onStep,
  canPrev,
  canNext,
  onClose,
}: {
  file: WorkspaceFile;
  onStep: (d: -1 | 1) => void;
  canPrev: boolean;
  canNext: boolean;
  onClose: () => void;
}) {
  const { message } = AntdApp.useApp();
  const session = useWorkspacePreview(true);
  const [view, setView] = useState<View>("preview");
  useEffect(() => setView("preview"), [file.path]);

  const kind = previewKind(file.path, file.size);
  const url = session.data ? previewFileUrl(session.data.base_url, file.path) : undefined;
  const needsText =
    kind === "markdown" || kind === "text" || kind === "unknown" || (kind === "html" && view === "source");
  const textQ = usePreviewText(needsText ? url : undefined, file.etag);

  // 令牌失效（放置超过一小时）：换一个令牌重试一次
  const renewed = useRef(false);
  useEffect(() => {
    if (textQ.error instanceof PreviewFetchError && textQ.error.status === 410 && !renewed.current) {
      renewed.current = true;
      void session.refetch();
    }
  }, [textQ.error, session]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest("input, textarea, .ant-modal")) return;
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowDown" && canNext) {
        e.preventDefault();
        onStep(1);
      }
      if (e.key === "ArrowUp" && canPrev) {
        e.preventDefault();
        onStep(-1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, onStep, canPrev, canNext]);

  const download = () =>
    downloadWorkspaceFile(file.path).catch((e: Error) => message.error(e.message));
  const downloadButton = (
    <Button icon={<Icon name="download" size={14} />} onClick={() => void download()}>
      下载
    </Button>
  );
  const dir = file.path.includes("/") ? file.path.slice(0, file.path.lastIndexOf("/")) : "";

  const body = (() => {
    if (session.error) return <Message text={`无法打开预览：${(session.error as Error).message}`} />;
    if (!url) return <Loading />;
    switch (kind) {
      case "empty":
        return <Message text="空文件" />;
      case "pdf":
        return <PdfFrame url={url} title={file.path} />;
      case "image":
        if (file.size > IMAGE_MAX_BYTES) {
          return <Message text={`图片过大（${formatBytes(file.size)}），请下载查看`}>{downloadButton}</Message>;
        }
        return <ImageView key={file.etag} url={`${url}?v=${file.etag ?? ""}`} alt={file.path} />;
      case "html":
        if (view === "preview") return <HtmlFrame url={url} title={file.path} />;
        break;
    }
    if (!needsText) return <Message text="这种文件不能在线预览">{downloadButton}</Message>;
    if (textQ.isPending) return <Loading />;
    if (textQ.error) {
      return (
        <div style={{ padding: "var(--s-5)" }}>
          <Alert type="error" showIcon message="读取失败" description={(textQ.error as Error).message} />
        </div>
      );
    }
    const data = textQ.data;
    if (data.binary) return <Message text="这种文件不能在线预览">{downloadButton}</Message>;
    if (kind === "markdown" && view === "preview") return <MarkdownView data={data} />;
    return <TextView data={data} wrap={false} formatJson={extOf(file.path) === "json"} />;
  })();

  return (
    <aside className={styles.pv} aria-label="文件预览">
      <div className={styles.pvHead}>
        <Icon name="file" size={16} style={{ color: "var(--fg-3)", flex: "none" }} />
        <div className={styles.pvName}>
          <div className={styles.t} title={file.path}>
            {file.name}
          </div>
          <div className={styles.m}>
            {formatBytes(file.size)} · {relativeTime(file.modified_at)}
            {dir && ` · ${dir}/`}
          </div>
        </div>
        {(kind === "html" || kind === "markdown") && (
          <Segmented<View>
            size="small"
            value={view}
            onChange={setView}
            options={[
              { label: "预览", value: "preview" },
              { label: "源码", value: "source" },
            ]}
          />
        )}
        <Tooltip title="上一个（↑）">
          <Button type="text" disabled={!canPrev} icon={<Icon name="up" size={14} />} onClick={() => onStep(-1)} />
        </Tooltip>
        <Tooltip title="下一个（↓）">
          <Button type="text" disabled={!canNext} icon={<Icon name="down" size={14} />} onClick={() => onStep(1)} />
        </Tooltip>
        {url && (
          <Tooltip title="新标签打开">
            <Button
              type="text"
              icon={<Icon name="globe" size={14} />}
              href={url}
              target="_blank"
              rel="noopener noreferrer"
            />
          </Tooltip>
        )}
        <Tooltip title="下载">
          <Button type="text" icon={<Icon name="download" size={14} />} onClick={() => void download()} />
        </Tooltip>
        <Tooltip title="关闭（Esc）">
          <Button type="text" icon={<Icon name="x" size={14} />} onClick={onClose} />
        </Tooltip>
      </div>
      <div className={styles.pvBody}>{body}</div>
    </aside>
  );
}
