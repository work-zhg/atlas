"use client";

/** 流程空间里一个文件的当前内容：文本直接显示（截前 512 KB），其余给下载 */
import { useQuery } from "@tanstack/react-query";
import { Spin } from "antd";
import type { ReactNode } from "react";

import { apiUrl } from "@/lib/api";

import { Icon } from "./ui";

const TEXT = /\.(md|markdown|txt|json|ya?ml|csv|tsv|py|ts|tsx|js|jsx|java|go|rs|sql|sh|patch|diff|html|css|xml|toml|ini|log|env)$/i;
const LIMIT = 512 * 1024;

export function downloadUrl(processId: string, path: string): string {
  return apiUrl(`/processes/${processId}/space/download`, { path });
}

export function FilePreview({ processId, path, label }: { processId: string; path: string; label?: ReactNode }) {
  const textual = TEXT.test(path);
  const q = useQuery({
    queryKey: ["process", processId, "space", "file", path],
    queryFn: async () => {
      const res = await fetch(downloadUrl(processId, path), { credentials: "same-origin" });
      if (!res.ok) throw new Error(res.status === 404 ? "文件不存在（可能被删了或改了名）" : `读不出来：${res.status}`);
      const buf = await res.arrayBuffer();
      const text = new TextDecoder().decode(buf.slice(0, LIMIT));
      return { text, cut: buf.byteLength > LIMIT };
    },
    enabled: textual,
    staleTime: 5_000,
  });
  return (
    <div>
      <div className="dochead">
        <Icon name="file" size={14} />
        <b style={{ color: "var(--fg)" }} className="ellipsis">
          {path}
        </b>
        {label && <span className="rtag">{label}</span>}
        <span className="sp" />
        <a href={downloadUrl(processId, path)} title="下载">
          <Icon name="download" size={14} />
        </a>
      </div>
      {!textual ? (
        <div className="pcard sm muted">这种文件不能直接预览，点右上角下载</div>
      ) : q.isPending ? (
        <Spin />
      ) : q.error ? (
        <div className="pcard sm muted">{(q.error as Error).message}</div>
      ) : (
        <div className="doc">
          {q.data.text || "（空文件）"}
          {q.data.cut && <div className="xs faint">……只显示了前 512 KB，完整内容请下载</div>}
        </div>
      )}
    </div>
  );
}
