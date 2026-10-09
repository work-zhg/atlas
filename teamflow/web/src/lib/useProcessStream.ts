"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

/**
 * 订阅流程实时事件（SSE）：
 * - process 事件（落库的，带游标）→ 刷新流程与对应节点；断线后浏览器带 Last-Event-ID 自动续传；
 * - delta 事件（Agent 输出增量，不落库）→ 逐段拼到「正在输出」。
 */
export function useProcessStream(processId: string, startSeq: number | undefined) {
  const qc = useQueryClient();
  const [live, setLive] = useState<Record<string, string>>({});
  const started = useRef(false);

  useEffect(() => {
    if (startSeq === undefined || started.current) return;
    started.current = true;
    const es = new EventSource(`/api/v1/processes/${processId}/stream?after=${startSeq}`);
    es.addEventListener("process", (e) => {
      const ev = JSON.parse((e as MessageEvent).data) as { type: string; node_id: string | null };
      void qc.invalidateQueries({ queryKey: ["process", processId] });
      if (ev.node_id) void qc.invalidateQueries({ queryKey: ["node", processId, ev.node_id] });
      if (ev.node_id && (ev.type === "agent.replied" || ev.type === "agent.failed" || ev.type === "agent.started"))
        setLive((m) => ({ ...m, [ev.node_id!]: "" }));
    });
    es.addEventListener("delta", (e) => {
      const d = JSON.parse((e as MessageEvent).data) as { node_id: string; text: string };
      setLive((m) => ({ ...m, [d.node_id]: (m[d.node_id] ?? "") + d.text }));
    });
    return () => {
      es.close();
      started.current = false;
    };
  }, [processId, startSeq, qc]);

  return live;
}
