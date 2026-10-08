"use client";

import { Alert, Empty, Skeleton } from "antd";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { fetchPreviewText, PreviewFetchError, type PreviewText } from "@/api/threads";
import { Markdown } from "@/components/Markdown";
import chatStyles from "@/features/chat/chat.module.css";
import { formatBytes } from "./format";
import { TEXT_MAX_BYTES } from "./previewKind";
import styles from "./files.module.css";

/**
 * ★ 与后端 api/v1/previews.py 的 SANDBOX 是同一份清单。
 *
 *   **永远不要加 allow-same-origin。** 预览的 HTML 是模型写的，一律不可信；
 *   allow-same-origin 与 allow-scripts 同时出现时，页面里的脚本可以把自己的
 *   sandbox 属性删掉，隔离形同虚设。没有它，文档运行在不透明 origin，读不到
 *   平台页面，也不能借用户身份调 API。
 */
const SANDBOX = "allow-scripts allow-forms allow-modals allow-popups allow-downloads";

export function HtmlFrame({ url, title }: { url: string; title: string }) {
  return (
    <iframe
      className={styles.frame}
      src={url}
      title={title}
      sandbox={SANDBOX}
      referrerPolicy="no-referrer"
    />
  );
}

/** PDF：Chrome 的查看器在 sandbox 里不工作（§13 #3），后端对 PDF 也不下发 sandbox。 */
export function PdfFrame({ url, title }: { url: string; title: string }) {
  return <iframe className={styles.frame} src={url} title={title} referrerPolicy="no-referrer" />;
}

export function ImageView({ url, alt }: { url: string; alt: string }) {
  const [actual, setActual] = useState(false);
  return (
    <div className={styles.imageStage}>
      {/* eslint-disable-next-line @next/next/no-img-element -- 工作区文件，不经 next/image 优化 */}
      <img
        src={url}
        alt={alt}
        referrerPolicy="no-referrer"
        className={`${styles.image} ${actual ? styles.actual : ""}`}
        onClick={() => setActual((v) => !v)}
        title={actual ? "适应宽度" : "原始尺寸"}
      />
    </div>
  );
}

/**
 * 读取文本（前 1 MiB）。
 *
 * ★ queryKey 里有 url（含令牌）与 etag：令牌换了、文件变了都会重新取。
 *   运行中文件在变 —— 文本类自动刷新是想要的效果（§09）。
 */
export function usePreviewText(url: string | undefined, etag: string | null | undefined) {
  return useQuery({
    queryKey: ["preview", "text", url ?? "", etag ?? ""],
    queryFn: () => fetchPreviewText(url!, TEXT_MAX_BYTES),
    enabled: Boolean(url),
    staleTime: Infinity,
    retry: (count, err) => !(err instanceof PreviewFetchError) && count < 2,
  });
}

function TruncatedNote({ data }: { data: PreviewText }) {
  if (!data.truncated) return null;
  return (
    <Alert
      type="warning"
      showIcon
      banner
      message={`只显示前 ${formatBytes(TEXT_MAX_BYTES)}，文件共 ${formatBytes(data.total)}。完整内容请下载。`}
    />
  );
}

export function TextView({
  data,
  wrap,
  formatJson,
}: {
  data: PreviewText;
  wrap: boolean;
  formatJson: boolean;
}) {
  const text = useMemo(() => {
    if (!formatJson || data.truncated) return data.text;
    try {
      return JSON.stringify(JSON.parse(data.text), null, 2);
    } catch {
      return data.text; // 不是合法 JSON 就原样显示
    }
  }, [data, formatJson]);
  // 行号只在不换行时显示：换行后一行正文会占多行，行号就对不上了
  const gutter = useMemo(() => {
    if (wrap) return "";
    const n = text.split("\n").length;
    return Array.from({ length: n }, (_, i) => i + 1).join("\n");
  }, [text, wrap]);
  return (
    <>
      <TruncatedNote data={data} />
      <div className={styles.code}>
        {!wrap && <pre className={styles.gutter}>{gutter}</pre>}
        <pre className={`${styles.lines} ${wrap ? styles.wrap : ""}`}>{text}</pre>
      </div>
    </>
  );
}

export function MarkdownView({ data }: { data: PreviewText }) {
  return (
    <>
      <TruncatedNote data={data} />
      <div className={styles.markdown}>
        {/* ★ 复用对话里的 Markdown：已 sanitize、禁 img、外链强制 noopener */}
        <Markdown className={chatStyles.prose}>{data.text}</Markdown>
      </div>
    </>
  );
}

export function Loading() {
  return (
    <div style={{ padding: "var(--s-6)" }}>
      <Skeleton active paragraph={{ rows: 8 }} />
    </div>
  );
}

export function Message({ text, children }: { text: string; children?: React.ReactNode }) {
  return (
    <div className={styles.center}>
      <Empty description={text} image={Empty.PRESENTED_IMAGE_SIMPLE}>
        {children}
      </Empty>
    </div>
  );
}
