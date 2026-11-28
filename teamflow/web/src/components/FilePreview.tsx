"use client";

/**
 * 流程文件的当前内容（预览页用，在新标签页打开）：
 * Markdown 渲染成文档；代码、文本等宽显示；图片、PDF 直接看；其余给下载。
 *
 * ★ 文件不做版本管理：永远是当前内容。文本最多读 2 MB。
 * ★ HTML 只显示源码，不渲染（数字员工写的东西一律当不可信内容）。
 */
import { useQuery } from "@tanstack/react-query";
import { Spin } from "antd";
import { useEffect, useState } from "react";

import { Markdown } from "@atlas/chat-ui";

import { apiUrl, refreshSession, ucLoginUrl } from "@/lib/api";

const MARKDOWN = /\.(md|markdown)$/i;
const TEXT = /\.(txt|json|ya?ml|csv|tsv|py|ts|tsx|js|jsx|java|go|rs|sql|sh|patch|diff|html?|css|xml|toml|ini|log|env|conf|properties|kt|swift|c|h|cpp|hpp|rb|php)$/i;
const IMAGE: Record<string, string> = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp" };
const LIMIT = 2 * 1024 * 1024;

export type FileKind = "markdown" | "text" | "image" | "pdf" | "other";

export function kindOf(path: string): FileKind {
  if (MARKDOWN.test(path)) return "markdown";
  if (TEXT.test(path)) return "text";
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  if (IMAGE[ext]) return "image";
  if (ext === "pdf") return "pdf";
  return "other";
}

export function downloadUrl(processId: string, path: string): string {
  return apiUrl(`/processes/${processId}/space/download`, { path });
}

async function fetchFile(processId: string, path: string): Promise<ArrayBuffer> {
  const get = () => fetch(downloadUrl(processId, path), { credentials: "same-origin" });
  let res = await get();
  if (res.status === 401 && (await refreshSession())) res = await get();
  if (res.status === 401) {
    window.location.assign(ucLoginUrl());
    throw new Error("登录过期了");
  }
  if (res.status === 404) throw new Error("文件不存在（可能被删了或改了名）");
  if (res.status === 403) throw new Error("你看不到这条流程的文件");
  if (!res.ok) throw new Error(`读不出来（${res.status}）`);
  return res.arrayBuffer();
}

export function FileViewer({ processId, path }: { processId: string; path: string }) {
  const kind = kindOf(path);
  const q = useQuery({
    queryKey: ["process", processId, "space", "file", path],
    queryFn: () => fetchFile(processId, path),
    enabled: kind !== "other",
    staleTime: 5_000,
    retry: false,
  });
  if (kind === "other") return <div className="pcard sm muted">这种文件不能直接预览，点右上角「下载」</div>;
  if (q.isPending) return <Spin style={{ display: "block", margin: "80px auto" }} />;
  if (q.error) return <div className="pcard sm muted">{(q.error as Error).message}</div>;
  const buf = q.data;
  if (kind === "image" || kind === "pdf") return <Binary buf={buf} path={path} kind={kind} />;
  const cut = buf.byteLength > LIMIT;
  const text = new TextDecoder().decode(buf.slice(0, LIMIT));
  return (
    <>
      {!text.trim() ? (
        <div className="pcard sm muted">（空文件）</div>
      ) : kind === "markdown" ? (
        <Markdown className="mdoc">{text}</Markdown>
      ) : (
        <pre className="codedoc">{text}</pre>
      )}
      {cut && <div className="xs faint" style={{ marginTop: 8 }}>文件太大，只显示了前 2 MB，完整内容请下载</div>}
    </>
  );
}

function Binary({ buf, path, kind }: { buf: ArrayBuffer; path: string; kind: "image" | "pdf" }) {
  const [url, setUrl] = useState<string>();
  useEffect(() => {
    const ext = path.split(".").pop()?.toLowerCase() ?? "";
    const u = URL.createObjectURL(new Blob([buf], { type: kind === "pdf" ? "application/pdf" : IMAGE[ext] }));
    setUrl(u);
    return () => URL.revokeObjectURL(u);
  }, [buf, path, kind]);
  if (!url) return null;
  if (kind === "pdf") return <iframe src={url} title={path} style={{ width: "100%", height: "calc(100vh - 120px)", border: "1px solid var(--line)", borderRadius: 10 }} />;
  // eslint-disable-next-line @next/next/no-img-element -- 本地 blob 地址，不走图片优化
  return <img src={url} alt={path} style={{ maxWidth: "100%", border: "1px solid var(--line)", borderRadius: 10 }} />;
}
