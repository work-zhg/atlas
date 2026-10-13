"use client";

/**
 * 会话文件预览页 —— 从文件面板**新标签**打开（/files/{threadId}?path=…）。
 *
 * ★ 文件内容一律经预览站点读取（GET /v1/previews/{token}/…），不走 download：
 *   iframe / img 带不了 X-User-Id，令牌在 URL 路径里（§06）。
 * ★ 运行中文件在变：文本、图片随 etag 自动刷新；HTML 只提示「已更新」，
 *   点了才重载 —— 自动刷新会把用户在原型里点到的状态清掉（§09）。
 */
import { App as AntdApp, Alert, Button, Segmented, Switch, Tooltip } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  downloadThreadFile,
  PreviewFetchError,
  previewFileUrl,
  usePreviewSession,
  useThreadFiles,
} from "@/api/threads";
import { Icon } from "@/components/Icon";
import { relativeTime } from "@/lib/format";
import { ApiError } from "@/lib/http";
import { formatBytes } from "./format";
import { extOf, IMAGE_MAX_BYTES, previewKind } from "./previewKind";
import {
  HtmlFrame,
  ImageView,
  Loading,
  MarkdownView,
  Message,
  PdfFrame,
  TextView,
  usePreviewText,
} from "./renderers";
import styles from "./files.module.css";

type View = "preview" | "source";

export function FilePreviewPage({ threadId, path }: { threadId: string; path: string }) {
  const { message } = AntdApp.useApp();
  // 一直轮询（标签页在后台时 react-query 自动暂停）：预览页不知道 run 是否在跑
  const filesQ = useThreadFiles(threadId, true);
  const session = usePreviewSession(threadId);

  const files = filesQ.data?.data ?? [];
  const entry = files.find((f) => f.path === path);
  const kind = previewKind(path, entry?.size);
  const url = session.data ? previewFileUrl(session.data.base_url, path) : undefined;
  const slash = path.lastIndexOf("/");
  const dir = slash >= 0 ? path.slice(0, slash + 1) : "";
  const name = path.slice(slash + 1);

  const [view, setView] = useState<View>("preview");
  const [wrap, setWrap] = useState(false);
  const [formatJson, setFormatJson] = useState(true);
  const [frameKey, setFrameKey] = useState(0);
  const [downloading, setDownloading] = useState(false);

  // HTML 是一个站点：任何一个文件变了都算「已更新」，不只看入口文件
  const siteSig = files.map((f) => `${f.path}:${f.etag ?? ""}`).join("|");
  const [loadedSig, setLoadedSig] = useState<string>();
  useEffect(() => {
    if (loadedSig === undefined && filesQ.isSuccess) setLoadedSig(siteSig);
  }, [loadedSig, filesQ.isSuccess, siteSig]);
  const siteChanged = kind === "html" && view === "preview" && loadedSig !== undefined && siteSig !== loadedSig;

  const needsText =
    kind === "markdown" || kind === "text" || kind === "unknown" || (kind === "html" && view === "source");
  const textQ = usePreviewText(needsText ? url : undefined, entry?.etag);

  // 令牌失效（放置超过一小时）：换一个令牌重试一次。只试一次 —— 新令牌也 410
  // 说明不是过期问题，再换只会原地打转。
  const renewed = useRef(false);
  useEffect(() => {
    if (textQ.error instanceof PreviewFetchError && textQ.error.status === 410 && !renewed.current) {
      renewed.current = true;
      void session.refetch();
    }
  }, [textQ.error, session]);

  useEffect(() => {
    document.title = `${name || "文件"} · 文件预览`;
  }, [name]);

  const reload = useCallback(async () => {
    renewed.current = false;
    await session.refetch(); // 新令牌 → 文本 query 的 key 变了，自动重取
    setFrameKey((k) => k + 1);
    setLoadedSig(siteSig);
    void filesQ.refetch();
  }, [session, siteSig, filesQ]);

  const download = async () => {
    setDownloading(true);
    try {
      await downloadThreadFile(threadId, path);
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setDownloading(false);
    }
  };

  const hasSource = kind === "html" || kind === "markdown";
  const showsText = needsText && !(kind === "markdown" && view === "preview");
  const isJson = extOf(path) === "json";
  const downloadButton = (
    <Button icon={<Icon name="download" size={14} />} loading={downloading} onClick={() => void download()}>
      下载
    </Button>
  );

  const body = (() => {
    if (!path) return <Message text="缺少文件路径" />;
    if (session.error) {
      // ★ 路由级 404（FastAPI 默认的 {"detail":"Not Found"}，kind=http_404）= 后端没有
      //   这个接口，通常是 server 版本旧了。会话不存在、没配对象存储走的是平台错误信封，
      //   带具体的中文原因，原样显示。
      const noRoute = session.error instanceof ApiError && session.error.kind === "http_404";
      return (
        <Message
          text={
            noRoute
              ? "后端不支持文件预览（接口 404），请确认 server 已更新到包含预览功能的版本"
              : `无法打开预览：${session.error.message}`
          }
        />
      );
    }
    if (filesQ.data && !filesQ.data.configured) {
      return <Message text="这个部署没有配置对象存储，会话没有文件能力" />;
    }
    // 列表完整却没有它 = 被删了（列表被截断时不下这个结论，直接按路径去取）
    if (filesQ.isSuccess && !filesQ.data.truncated && !entry) {
      return <Message text="文件已不存在" />;
    }
    if (!url || (!entry && filesQ.isPending)) return <Loading />;

    switch (kind) {
      case "empty":
        return <Message text="空文件" />;
      case "pdf":
        return <PdfFrame url={url} title={path} />;
      case "image":
        if ((entry?.size ?? 0) > IMAGE_MAX_BYTES) {
          return <Message text={`图片过大（${formatBytes(entry!.size)}），请下载查看`}>{downloadButton}</Message>;
        }
        // ?v=etag：同一个 src 的 <img> 不会重新请求，换了内容要换地址
        return <ImageView key={entry?.etag} url={`${url}?v=${entry?.etag ?? ""}`} alt={path} />;
      case "html":
        if (view === "preview") return <HtmlFrame key={frameKey} url={url} title={path} />;
        break;
    }

    if (textQ.isPending) return <Loading />;
    if (textQ.error) {
      return (
        <div style={{ padding: "var(--s-5)" }}>
          <Alert type="error" showIcon message="读取失败" description={textQ.error.message} />
        </div>
      );
    }
    const data = textQ.data;
    if (data.binary) return <Message text="二进制文件，不支持预览">{downloadButton}</Message>;
    if (kind === "markdown" && view === "preview") return <MarkdownView data={data} />;
    return <TextView data={data} wrap={wrap} formatJson={isJson && formatJson} />;
  })();

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <Icon name="file" size={16} style={{ color: "var(--fg-3)", flex: "none" }} />
        <span className={styles.path} title={path}>
          {dir && <span className={styles.dir}>{dir}</span>}
          {name}
        </span>
        {entry && (
          <span className={styles.meta} title={entry.modified_at ?? undefined}>
            {formatBytes(entry.size)} · {relativeTime(entry.modified_at)}
          </span>
        )}

        <div className={styles.actions}>
          {hasSource && (
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
          {showsText && (
            <span className={styles.meta} style={{ display: "flex", alignItems: "center", gap: 6 }}>
              换行 <Switch size="small" checked={wrap} onChange={setWrap} />
            </span>
          )}
          {showsText && isJson && (
            <span className={styles.meta} style={{ display: "flex", alignItems: "center", gap: 6 }}>
              格式化 <Switch size="small" checked={formatJson} onChange={setFormatJson} />
            </span>
          )}
          <Tooltip title="重新加载">
            <Button
              type="text"
              aria-label="重新加载"
              icon={<Icon name="retry" size={14} />}
              loading={session.isFetching}
              onClick={() => void reload()}
            />
          </Tooltip>
          {url && (
            <Tooltip title="打开原始文件（HTML 会以无工具栏的整页打开）">
              <Button
                type="text"
                aria-label="打开原始文件"
                icon={<Icon name="globe" size={14} />}
                href={url}
                target="_blank"
                rel="noopener noreferrer"
              />
            </Tooltip>
          )}
          {downloadButton}
        </div>
      </header>

      {siteChanged && (
        <div className={styles.banner}>
          <Icon name="warn" size={14} />
          页面的文件已更新
          <Button size="small" onClick={() => void reload()}>
            重新加载
          </Button>
        </div>
      )}

      <div className={styles.body}>{body}</div>
    </div>
  );
}
