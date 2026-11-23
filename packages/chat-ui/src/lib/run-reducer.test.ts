/**
 * 会话流的去重、补缺口与重连游标（run-reducer 的 applyEvent / resumeCursor）。
 *
 * 用一个前端不渲染的事件类型：只走序号那一段逻辑（规则 3：未知类型只推进 lastSeq）。
 */
import { describe, expect, it } from "vitest";

import type { AnyTraceEvent } from "./events";
import { applyEvent, initialRunState, resumeCursor, type RunState } from "./run-reducer";

function ev(threadSeq: number): AnyTraceEvent {
  return {
    seq: threadSeq,
    thread_seq: threadSeq,
    run_id: "r1",
    ts: "2026-11-23T00:00:00Z",
    depth: 0,
    type: "test.noop",
    data: {},
  };
}

function feed(...seqs: number[]): RunState {
  return seqs.reduce((state, s) => applyEvent(state, ev(s)), initialRunState);
}

describe("applyEvent 的序号", () => {
  it("收过的再来一次直接丢掉（重连边界的重发）", () => {
    const state = feed(1, 2, 3);
    expect(applyEvent(state, ev(2))).toBe(state);
  });

  it("跳过的序号记下，晚到时照收，收过就不再要", () => {
    // ★ 终态 / 挂起事件先领序号、落库后才发布：父 run 的 run.suspended 比子 run 的头几条小、却晚到
    let state = feed(4, 5, 8);
    expect(state.lastSeq).toBe(8);
    expect(state.missingSeqs).toEqual([6, 7]);

    state = applyEvent(state, ev(6));
    expect(state.missingSeqs).toEqual([7]);
    expect(state.lastSeq).toBe(8);
    expect(applyEvent(state, ev(6))).toBe(state);
  });

  it("首连从回放窗口中间开始，前面的不算缺", () => {
    expect(feed(40, 41).missingSeqs).toEqual([]);
  });
});

describe("resumeCursor", () => {
  it("没有缺口：从收到过的最大序号之后接着要", () => {
    expect(resumeCursor(feed(1, 2, 3))).toBe(3);
  });

  it("有缺口：从最早的缺口之前接着要，晚发布的那条断线期间发出也能补回来", () => {
    expect(resumeCursor(feed(4, 5, 8))).toBe(5);
  });

  it("离最新太远的缺口不再往回要：补不上的缺口不能让每次重连都回放一大段", () => {
    const state: Pick<RunState, "lastSeq" | "missingSeqs"> = { lastSeq: 1000, missingSeqs: [3, 950] };
    expect(resumeCursor(state)).toBe(949);
    expect(resumeCursor({ lastSeq: 1000, missingSeqs: [3] })).toBe(1000);
  });
});
