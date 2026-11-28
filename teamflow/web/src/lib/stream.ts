"use client";

/**
 * 订阅流程实时事件（GET /processes/{id}/stream，SSE）：有新事件就刷新流程、对应节点、流程空间。
 *
 * ★ 用 fetch 读流而不是 EventSource：后端按事件类型发 `event: node.started` 之类的命名事件，
 *   EventSource 要逐个类型注册；令牌过期的 401 也要先刷新登录态再连（EventSource 遇到非 200 直接关）。
 * ★ 从「已经拿到的最大序号」之后接着要：先读一次事件列表，连上以后只收新的。
 * ★ Agent 的逐段输出不走这里：节点页的对话组件直接订阅 Agent 平台的会话流。
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { apiUrl, refreshSession } from "./api";
import type { ProcessEvent } from "./types";

const BACKOFF = [1000, 2000, 5000, 10000];

export function useProcessStream(processId: string | undefined, after: number | undefined) {
  const qc = useQueryClient();
  // 只用第一次拿到的游标：之后的新事件由这条连接自己推进，不因为列表变了重连
  const start = useRef<{ pid?: string; seq?: number }>({});
  if (start.current.pid !== processId) start.current = { pid: processId };
  if (after !== undefined && start.current.seq === undefined) start.current.seq = after;
  const ready = start.current.seq !== undefined;

  useEffect(() => {
    if (!processId || !ready) return;
    const ctl = new AbortController();
    let cursor = start.current.seq ?? 0;
    let tries = 0;

    const onEvent = (ev: ProcessEvent) => {
      cursor = Math.max(cursor, ev.seq);
      qc.setQueryData<ProcessEvent[]>(["process", processId, "events"], (old) =>
        old && !old.some((e) => e.seq === ev.seq) ? [...old, ev] : old,
      );
      void qc.invalidateQueries({ queryKey: ["process", processId], exact: true });
      if (ev.node_id) void qc.invalidateQueries({ queryKey: ["node", processId, ev.node_id] });
      if (ev.type.startsWith("submission") || ev.type === "space.uploaded")
        void qc.invalidateQueries({ queryKey: ["process", processId, "space"] });
      if (ev.type.startsWith("review") || ev.type.startsWith("vote")) void qc.invalidateQueries({ queryKey: ["todos"] });
    };

    const connect = async (): Promise<void> => {
      while (!ctl.signal.aborted) {
        try {
          const res = await fetch(apiUrl(`/processes/${processId}/stream`, { after: cursor }), {
            credentials: "same-origin",
            headers: { Accept: "text/event-stream" },
            signal: ctl.signal,
          });
          if (res.status === 401 && (await refreshSession())) continue;
          if (!res.ok || !res.body) {
            if (res.status >= 400 && res.status < 500) return; // 看不到了 / 不存在：重试也没用
            throw new Error(`stream ${res.status}`);
          }
          tries = 0;
          const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
          let buf = "";
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += value;
            let i: number;
            while ((i = buf.indexOf("\n\n")) >= 0) {
              const frame = buf.slice(0, i);
              buf = buf.slice(i + 2);
              const data = frame
                .split("\n")
                .filter((l) => l.startsWith("data:"))
                .map((l) => l.slice(5).trimStart())
                .join("\n");
              if (data) onEvent(JSON.parse(data) as ProcessEvent);
            }
          }
        } catch {
          if (ctl.signal.aborted) return;
        }
        await new Promise((r) => setTimeout(r, BACKOFF[Math.min(tries++, BACKOFF.length - 1)]));
      }
    };
    void connect();
    return () => ctl.abort();
  }, [processId, ready, qc]);
}
