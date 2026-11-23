/**
 * 会话流的 SSE 客户端：断线时报「重新连接中」，重连按调用方给的游标接着要。
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { streamThreadEvents } from "./sse";

function frame(threadSeq: number): string {
  const event = {
    seq: threadSeq,
    thread_seq: threadSeq,
    run_id: "r1",
    ts: "2026-11-23T00:00:00Z",
    depth: 0,
    type: "test.noop",
    data: {},
  };
  return `id: ${threadSeq}\ndata: ${JSON.stringify(event)}\n\n`;
}

/** 一次连接：发完这些帧就断开（会话流不会正常结束，断开一律当意外）。 */
function sseResponse(frames: string[]): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(new TextEncoder().encode(f));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("streamThreadEvents", () => {
  it("断线时回调 onDisconnect，重连时带调用方给的游标", async () => {
    const asked: (string | null)[] = [];
    const ctrl = new AbortController();
    let calls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        asked.push(new Headers(init.headers).get("Last-Event-ID"));
        calls += 1;
        if (calls === 1) return sseResponse([frame(4), frame(5), frame(8)]);
        ctrl.abort(); // 第二次连上就够了
        return sseResponse([]);
      }),
    );

    const seen: number[] = [];
    const events: string[] = [];
    await streamThreadEvents({
      client: { apiBase: "http://atlas.test" },
      threadId: "t1",
      signal: ctrl.signal,
      onEvent: (e) => seen.push(e.thread_seq),
      onOpen: () => events.push("open"),
      onDisconnect: () => events.push("disconnect"),
      // 6、7 还没到：从 5 之后接着要（不是收到过的最大值 8）
      cursor: () => (seen.length ? 5 : 0),
    });

    expect(seen).toEqual([4, 5, 8]);
    expect(asked).toEqual([null, "5"]);
    expect(events.slice(0, 3)).toEqual(["open", "disconnect", "open"]);
  });

  it("不给游标时用收到过的最大序号", async () => {
    const asked: (string | null)[] = [];
    const ctrl = new AbortController();
    let calls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        asked.push(new Headers(init.headers).get("Last-Event-ID"));
        calls += 1;
        if (calls === 1) return sseResponse([frame(4), frame(8)]);
        ctrl.abort();
        return sseResponse([]);
      }),
    );

    await streamThreadEvents({
      client: { apiBase: "http://atlas.test" },
      threadId: "t1",
      signal: ctrl.signal,
      onEvent: () => {},
    });
    expect(asked).toEqual([null, "8"]);
  });
});
