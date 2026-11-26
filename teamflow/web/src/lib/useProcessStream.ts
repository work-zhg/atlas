"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { refreshSession } from "@atlas/chat-ui/src/lib/session";

import { API_BASE } from "./api";

/**
 * 订阅流程实时事件（SSE）：落库的流程事件（带游标）→ 刷新流程与对应节点；
 * 断线后浏览器带 Last-Event-ID 自动续传。
 * ★ 访问令牌过期时网关回 401：原生 EventSource 遇到非 200 直接关掉、不再重连。这时先刷新登录态，
 *   再从收到的最后一条游标重新订阅（刷新不了就不再连：页面上的请求会把人带去登录页）。
 *
 * ★ Agent 的逐段输出不走这里：对话区是 Atlas 对话组件，直接订阅节点的 Atlas 会话流（经代理）。
 */
export function useProcessStream(processId: string, startSeq: number | undefined) {
  const qc = useQueryClient();
  const started = useRef(false);

  useEffect(() => {
    if (startSeq === undefined || started.current) return;
    started.current = true;
    let es: EventSource | null = null;
    let after = startSeq;
    let stopped = false;
    const open = () => {
      es = new EventSource(`${API_BASE}/api/v1/processes/${processId}/stream?after=${after}`);
      es.addEventListener("process", (e) => {
        const msg = e as MessageEvent;
        if (msg.lastEventId) after = Number(msg.lastEventId) || after;
        const ev = JSON.parse(msg.data) as { type: string; node_id: string | null };
        void qc.invalidateQueries({ queryKey: ["process", processId] });
        if (ev.node_id) void qc.invalidateQueries({ queryKey: ["node", processId, ev.node_id] });
      });
      es.onerror = () => {
        // 还在自动重连（CONNECTING）就交给浏览器；被拒（CLOSED，如令牌过期的 401）才自己处理
        if (stopped || es?.readyState !== EventSource.CLOSED) return;
        void refreshSession().then((ok) => {
          if (ok && !stopped) open();
        });
      };
    };
    open();
    return () => {
      stopped = true;
      es?.close();
      started.current = false;
    };
  }, [processId, startSeq, qc]);
}
