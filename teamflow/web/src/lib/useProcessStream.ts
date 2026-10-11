"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { BASE_PATH } from "./api";

/**
 * 订阅流程实时事件（SSE）：落库的流程事件（带游标）→ 刷新流程与对应节点；
 * 断线后浏览器带 Last-Event-ID 自动续传。
 *
 * ★ Agent 的逐段输出不走这里：对话区是 Atlas 对话组件，直接订阅节点的 Atlas 会话流（经代理）。
 */
export function useProcessStream(processId: string, startSeq: number | undefined) {
  const qc = useQueryClient();
  const started = useRef(false);

  useEffect(() => {
    if (startSeq === undefined || started.current) return;
    started.current = true;
    const es = new EventSource(`${BASE_PATH}/api/v1/processes/${processId}/stream?after=${startSeq}`);
    es.addEventListener("process", (e) => {
      const ev = JSON.parse((e as MessageEvent).data) as { type: string; node_id: string | null };
      void qc.invalidateQueries({ queryKey: ["process", processId] });
      if (ev.node_id) void qc.invalidateQueries({ queryKey: ["node", processId, ev.node_id] });
    });
    return () => {
      es.close();
      started.current = false;
    };
  }, [processId, startSeq, qc]);
}
