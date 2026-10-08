"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { qk } from "@/api/keys";
import { applyEvent, initialRunState, type RunState } from "@/lib/run-reducer";
import { streamThreadEvents } from "@/lib/sse";

export interface UseThreadStreamOptions {
  threadId: string | undefined;
  /** 一轮结束后回调，用于滚动定位等副作用 */
  onTurnEnd?: (state: RunState) => void;
}

export interface UseThreadStreamResult {
  state: RunState;
  /** 连接中断、正在重试 —— UI 上给个"重新连接中"提示 */
  reconnecting: boolean;
  /** 不可恢复的连接错误（4xx 等） */
  streamError: Error | null;
  reset: () => void;
}

/**
 * 订阅一条**会话**的事件流并归约成状态。
 *
 * ★ 订阅单位是 thread，不是 run。这带来两处简化：
 *
 *   ① **不需要知道"当前是哪个 run"**。原先 activeRunId 只在「发消息成功」时
 *      赋值，重新挂载后是 undefined —— 于是不订阅任何流，而助手消息要到
 *      message.completed 才落库，长 run 期间页面上只剩用户那条提问，看着像
 *      「回答到一半全没了」。那段恢复逻辑现在整个消失：订阅会话天然就是恢复。
 *
 *   ② **流不会因为一轮结束而断**。用户发第二句话时流还在，委派挂起期间也在
 *      —— 后者正是子智能体的审批弹窗能出现的前提
 *      （doc/detail/suspension.html §06）。
 *
 * ★ 断线续传的关键：lastSeq 存在 ref 里而不是只在 state 里 ——
 *   重连回调需要读到**最新**值，而闭包捕获的 state 是旧的。
 */
export function useThreadStream({
  threadId,
  onTurnEnd,
}: UseThreadStreamOptions): UseThreadStreamResult {
  const qc = useQueryClient();
  const [state, setState] = useState<RunState>(initialRunState);
  const [reconnecting, setReconnecting] = useState(false);
  const [streamError, setStreamError] = useState<Error | null>(null);

  const lastSeqRef = useRef(0);
  const stateRef = useRef<RunState>(initialRunState);
  const onTurnEndRef = useRef(onTurnEnd);
  onTurnEndRef.current = onTurnEnd;

  /**
   * 手动清空实时状态与游标。
   *
   * ★ 切会话**不需要**调它 —— 那由订阅 effect 自己处理（见下）。留着是给
   *   「明确要丢掉已收事件」的场合用的。
   *
   * ★ 不要在发消息时调：游标归零之后，下一次断线重连会从默认窗口重新回放
   *   一遍。新一轮的清屏由 run.started 事件负责（reducer 的 freshTurn）。
   */
  const reset = useCallback(() => {
    lastSeqRef.current = 0;
    stateRef.current = initialRunState;
    setState(initialRunState);
    setStreamError(null);
    setReconnecting(false);
  }, []);

  useEffect(() => {
    if (!threadId) return;

    // ★ 换会话 = 换一套序号体系，游标必须在**这里**归零。
    //
    //   放在调用方的 useEffect 里不行：React 按声明顺序执行 effect，而本
    //   hook 的 effect 先跑 —— 新流会带着**上一条会话**的 thread_seq 去请求
    //   Last-Event-ID，服务端于是从一个毫无关系的位置开始回放（旧游标大就
    //   跳过新会话的开头，小就全量回放）。踩过一次。
    //
    //   断线重连不经过这里（在 streamThreadEvents 内部循环），所以游标不会
    //   被重连清掉。
    lastSeqRef.current = 0;
    stateRef.current = initialRunState;
    setState(initialRunState);

    const ctrl = new AbortController();
    setStreamError(null);
    // 每次连接开始时先标成"连接中"，onOpen 到达再复位
    setReconnecting(true);

    void streamThreadEvents({
      threadId,
      lastSeq: lastSeqRef.current,
      signal: ctrl.signal,
      onOpen: () => setReconnecting(false),
      onEvent: (event) => {
        setState((prev) => {
          const next = applyEvent(prev, event);
          lastSeqRef.current = next.lastSeq;
          stateRef.current = next;
          return next;
        });
      },
      onError: (err) => {
        setReconnecting(false);
        setStreamError(err);
      },
      onTurnEnd: (runId) => {
        // 文档 §10：一轮结束后回查 GET /runs/{id} 取最终用量 ——
        // 事件里的 usage 可能早于执行器落库。
        void qc.invalidateQueries({ queryKey: qk.runs.detail(runId) });
        // 助手消息此刻已落库，消息列表要刷新才能拿到持久化的那条
        void qc.invalidateQueries({ queryKey: qk.threads.messages(threadId) });
        void qc.invalidateQueries({ queryKey: qk.threads.all });
        onTurnEndRef.current?.(stateRef.current);
      },
    });

    // ★ 组件卸载 / 切会话时 abort —— 这是结束订阅的**唯一**方式。
    //   会话流不会自己结束，没有这一句就是一条泄漏的连接。
    return () => ctrl.abort();
  }, [threadId, qc]);

  return { state, reconnecting, streamError, reset };
}
