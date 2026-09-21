"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { qk } from "@/api/keys";
import { applyEvent, initialRunState, type RunState } from "@/lib/run-reducer";
import { streamRunEvents } from "@/lib/sse";

export interface UseRunStreamOptions {
  runId: string | undefined;
  threadId: string | undefined;
  /** 流结束后回调，用于滚动定位等副作用 */
  onFinished?: (state: RunState) => void;
}

export interface UseRunStreamResult {
  state: RunState;
  /** 连接中断、正在重试 —— UI 上给个"重新连接中"提示 */
  reconnecting: boolean;
  /** 不可恢复的连接错误（4xx 等） */
  streamError: Error | null;
  reset: () => void;
}

/**
 * 订阅一个 run 的事件流并归约成 RunState。
 *
 * 断线续传的关键：lastSeq 存在 ref 里而不是只在 state 里 ——
 * 重连回调需要读到**最新**值，而闭包捕获的 state 是旧的。
 */
export function useRunStream({
  runId,
  threadId,
  onFinished,
}: UseRunStreamOptions): UseRunStreamResult {
  const qc = useQueryClient();
  const [state, setState] = useState<RunState>(initialRunState);
  const [reconnecting, setReconnecting] = useState(false);
  const [streamError, setStreamError] = useState<Error | null>(null);

  const lastSeqRef = useRef(0);
  const onFinishedRef = useRef(onFinished);
  onFinishedRef.current = onFinished;

  const reset = useCallback(() => {
    lastSeqRef.current = 0;
    setState(initialRunState);
    setStreamError(null);
    setReconnecting(false);
  }, []);

  useEffect(() => {
    if (!runId) return;

    const ctrl = new AbortController();
    setStreamError(null);
    // 每次连接开始时先标成"连接中"，onOpen 到达再复位
    setReconnecting(true);

    let finalState: RunState = initialRunState;

    void streamRunEvents({
      runId,
      lastSeq: lastSeqRef.current,
      signal: ctrl.signal,
      onOpen: () => setReconnecting(false),
      onEvent: (event) => {
        setState((prev) => {
          const next = applyEvent(prev, event);
          lastSeqRef.current = next.lastSeq;
          finalState = next;
          return next;
        });
      },
      onError: (err) => {
        setReconnecting(false);
        setStreamError(err);
      },
      onClose: () => {
        setReconnecting(false);
        // 文档 §10：收到 run.finished 后回查 GET /runs/{id} 取最终用量 ——
        // 事件里的 usage 可能早于执行器落库。
        void qc.invalidateQueries({ queryKey: qk.runs.detail(runId) });
        if (threadId) {
          // 助手消息此刻已落库，消息列表要刷新才能拿到持久化的那条
          void qc.invalidateQueries({ queryKey: qk.threads.messages(threadId) });
          void qc.invalidateQueries({ queryKey: qk.threads.all });
        }
        onFinishedRef.current?.(finalState);
      },
    });

    return () => ctrl.abort();
  }, [runId, threadId, qc]);

  return { state, reconnecting, streamError, reset };
}
