/**
 * SSE 客户端 —— fetch 流式，手写帧解析与重连。
 *
 * 为什么不用原生 EventSource：后端身份走 X-User-Id header（identity.py），
 * EventSource 设不了任何自定义 header。今天靠 default_user_id 回落还能跑，
 * 但 §5.2 换成 JWT/SSO 时 Bearer token 无处安放。
 *
 * 为什么不用 @microsoft/fetch-event-source：它引用 window/document，
 * 在 Node 里直接 ReferenceError —— 意味着这段最吃契约的逻辑只能靠手点浏览器
 * 来验。自己写反而只多几十行，换来 scripts/verify-stream.ts 能对真实后端
 * 跑断线续传的回归。
 *
 * 三个容易写错的点：
 *   · 重连必须带 Last-Event-ID（= 已处理的最大 seq），否则从头重放，
 *     用户看到消息重复。
 *   · 收到终态事件后必须停止，不能再重连 —— 后端此时会关闭连接，
 *     误判成"意外断开"就会对一个已结束的 run 无限重试。
 *   · 4xx 不重试。run 不存在或无权访问，重试一万次还是 404。
 */
import { isTerminalEvent, parseTraceEvent, type AnyTraceEvent } from "./events";
import { ApiError, apiUrl, authHeaders } from "./http";

const SSE_MIME = "text/event-stream";

/** 退避序列（毫秒）。超出长度后固定用最后一个值。 */
const BACKOFF_MS = [500, 1000, 2000, 4000, 8000];

export interface RunStreamOptions {
  runId: string;
  /**
   * 从哪个 seq 之后开始收。页面刷新后从 0 开始 ——
   * 此时本地没有任何状态，需要后端完整回放。
   */
  lastSeq?: number;
  signal: AbortSignal;
  onEvent: (event: AnyTraceEvent) => void;
  /** 每次（重）连成功。用于复位 UI 上的"重新连接中"。 */
  onOpen?: () => void;
  /** 流正常结束（收到终态事件） */
  onClose?: () => void;
  /** 不可恢复的错误。可恢复错误在内部重试消化，不会走到这里。 */
  onError?: (error: Error) => void;
  /** 连续失败多少次后放弃。默认 6 次（约 25 秒）。 */
  maxRetries?: number;
}

class FatalError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** 把字节流切成 SSE 帧，逐帧回调。返回时表示连接已结束。 */
async function readFrames(
  body: ReadableStream<Uint8Array>,
  onFrame: (frame: string) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      // \r\n 归一化：规范允许三种换行，后端用 \n，但代理可能改写
      buf = (buf + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");

      let idx: number;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        onFrame(buf.slice(0, idx));
        buf = buf.slice(idx + 2);
      }
    }
  } finally {
    reader.cancel().catch(() => {});
  }
}

/** 从一帧里取出 data 行（可能多行，按规范用 \n 拼接）。 */
function frameData(frame: string): string | null {
  const parts: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("data:")) parts.push(line.slice(5).replace(/^ /, ""));
  }
  return parts.length > 0 ? parts.join("\n") : null;
}

/**
 * 订阅一个 run 的事件流。Promise 在流结束或放弃重试时 resolve。
 * 取消订阅：abort 传入的 signal。
 */
export async function streamRunEvents(opts: RunStreamOptions): Promise<void> {
  const { runId, signal, onEvent, onOpen, onClose, onError, maxRetries = 6 } = opts;

  let lastSeq = opts.lastSeq ?? 0;
  let terminated = false;
  let attempt = 0;

  while (!terminated && !signal.aborted) {
    try {
      const headers: Record<string, string> = { Accept: SSE_MIME, ...authHeaders() };
      // ★ 续传游标。首连时若 lastSeq>0（页面恢复场景）也会带上。
      if (lastSeq > 0) headers["Last-Event-ID"] = String(lastSeq);

      const res = await fetch(apiUrl(`/v1/runs/${runId}/events`), { headers, signal });

      if (res.status >= 400 && res.status < 500) {
        throw new FatalError(res.status, `事件流被拒绝（${res.status}）`);
      }
      if (!res.ok || !res.body) {
        throw new Error(`事件流连接失败（${res.status}）`);
      }

      attempt = 0; // 连上了就重置退避
      onOpen?.();

      await readFrames(res.body, (frame) => {
        const raw = frameData(frame);
        if (!raw) return; // 心跳 / 注释帧
        const event = parseTraceEvent(raw);
        if (!event) return; // 坏帧不该拖垮整条流

        // 去重交给 reducer（契约规则 2），这里只维护续传游标
        if (event.seq > lastSeq) lastSeq = event.seq;
        onEvent(event);
        if (isTerminalEvent(event.type)) terminated = true;
      });

      // 流结束但没见到终态 —— 意外断开，退避后带游标续传
    } catch (err) {
      if (signal.aborted) return; // 调用方主动取消，不是错误
      if (err instanceof FatalError) {
        onError?.(new ApiError(err.status, "sse_rejected", err.message));
        return;
      }
      // 其余当作可恢复，落到下面的退避逻辑
    }

    if (terminated || signal.aborted) break;

    if (++attempt > maxRetries) {
      onError?.(new Error(`事件流连接中断，已重试 ${maxRetries} 次`));
      return;
    }
    const delay = BACKOFF_MS[Math.min(attempt - 1, BACKOFF_MS.length - 1)]!;
    await new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, delay);
      signal.addEventListener("abort", () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
    });
  }

  if (!signal.aborted) onClose?.();
}
