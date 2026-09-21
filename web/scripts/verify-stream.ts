/**
 * 对真实后端验证 SSE 解析与事件归约 —— 默认不跑，需要后端在 :8000。
 *
 *     make web-verify        （或 cd web && pnpm verify:stream）
 *
 * 为什么需要它：events.ts / run-reducer.ts 是前端最吃契约的两个模块，
 * 而 tsc 只能保证字段名拼对，保证不了「后端真的这么发」。这里用真实
 * 模型跑一轮带工具的对话，断言：
 *   · seq 严格递增无空洞（契约规则 2）
 *   · todos 全量快照替换（规则 1）
 *   · 断线后带 Last-Event-ID 续传，归约结果与不断线时一致
 *
 * 这几个模块刻意不依赖 window/document，所以能脱离浏览器跑 ——
 * 这也是没有采用 @microsoft/fetch-event-source 的原因（它引用 window）。
 */
import { parseTraceEvent, type AnyTraceEvent } from "../src/lib/events";
import { applyEvent, initialRunState, type RunState } from "../src/lib/run-reducer";
import { streamRunEvents } from "../src/lib/sse";

const BASE = process.env.API_BASE ?? "http://127.0.0.1:8000";

let failures = 0;
function check(ok: boolean, label: string, extra = ""): void {
  console.log(`${ok ? "  ✓" : "  ✗"} ${label}${extra ? ` — ${extra}` : ""}`);
  if (!ok) failures++;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) throw new Error(`${path} → ${res.status} ${await res.text()}`);
  return (await res.json()) as T;
}

/** 读 SSE 流，最多读 maxEvents 条后主动断开。返回收到的事件。 */
async function readStream(
  runId: string,
  opts: { lastSeq?: number; maxEvents?: number } = {},
): Promise<AnyTraceEvent[]> {
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (opts.lastSeq) headers["Last-Event-ID"] = String(opts.lastSeq);

  const ctrl = new AbortController();
  const res = await fetch(`${BASE}/v1/runs/${runId}/events`, {
    headers,
    signal: ctrl.signal,
  });
  if (!res.body) throw new Error("无响应体");

  const events: AnyTraceEvent[] = [];
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  try {
    outer: while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      // SSE 以空行分帧
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);

        const dataLine = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!dataLine) continue;
        const ev = parseTraceEvent(dataLine.slice(6));
        if (!ev) continue;
        events.push(ev);

        if (opts.maxEvents && events.length >= opts.maxEvents) break outer;
        if (["run.finished", "run.failed", "run.cancelled"].includes(ev.type)) break outer;
      }
    }
  } finally {
    ctrl.abort();
    reader.cancel().catch(() => {});
  }
  return events;
}

function reduceAll(events: AnyTraceEvent[], from: RunState = initialRunState): RunState {
  return events.reduce(applyEvent, from);
}

async function main(): Promise<void> {
  console.log(`后端：${BASE}\n`);

  const agents = await api<{ data: { id: string; slug: string }[] }>("/v1/agents");
  const agent = agents.data.find((a) => a.slug === "e2e-analyst") ?? agents.data[0];
  if (!agent) throw new Error("没有可用智能体");

  // ---------- 场景 1：完整流 ----------
  console.log("场景 1 · 完整流（带工具调用）");
  const t1 = await api<{ id: string }>("/v1/threads", {
    method: "POST",
    body: JSON.stringify({ agent_id: agent.id, title: "" }),
  });
  const r1 = await api<{ run_id: string }>(`/v1/threads/${t1.id}/runs`, {
    method: "POST",
    body: JSON.stringify({
      content: [{ type: "text", text: "把 6*7 的结果写进 /verify.txt。先列两条待办。" }],
    }),
  });

  const events1 = await readStream(r1.run_id);
  const state1 = reduceAll(events1);

  const seqs = events1.map((e) => e.seq);
  check(
    seqs.every((s, i) => (i === 0 ? s === 1 : s === seqs[i - 1]! + 1)),
    "seq 从 1 严格递增无空洞（规则 2）",
    `共 ${seqs.length} 条`,
  );
  check(state1.status === "succeeded", "终态为 succeeded", state1.status);
  check(state1.text.length > 0, "有正文输出", `${state1.text.length} 字`);
  check(state1.todos.length >= 2, "计划页有数据（todos）", `${state1.todos.length} 条`);
  check(state1.toolCalls.length > 0, "工具页有数据", `${state1.toolCalls.length} 次调用`);
  check(
    state1.toolCalls.every((c) => c.status !== "running"),
    "所有工具调用都有终态",
  );
  check(
    state1.files.some((f) => f.path === "/verify.txt"),
    "文件页有 /verify.txt",
    state1.files.map((f) => f.path).join(", "),
  );
  check((state1.usage.totalTokens ?? 0) > 0, "用量已归约", String(state1.usage.totalTokens));

  const todoEvents = events1.filter((e) => e.type === "todos.updated");
  check(
    todoEvents.every((e) => Array.isArray((e.data as { todos?: unknown }).todos)),
    "todos 每次都是全量数组（规则 1）",
    `${todoEvents.length} 次快照`,
  );

  // ---------- 场景 2：断线续传 ----------
  console.log("\n场景 2 · 断线续传（Last-Event-ID）");
  const t2 = await api<{ id: string }>("/v1/threads", {
    method: "POST",
    body: JSON.stringify({ agent_id: agent.id, title: "" }),
  });
  const r2 = await api<{ run_id: string }>(`/v1/threads/${t2.id}/runs`, {
    method: "POST",
    body: JSON.stringify({
      content: [{ type: "text", text: "把 8*9 的结果写进 /resume.txt。先列两条待办。" }],
    }),
  });

  // 读 4 条就断
  const part1 = await readStream(r2.run_id, { maxEvents: 4 });
  const mid = reduceAll(part1);
  check(part1.length === 4, "已读取前 4 条后断开", `lastSeq=${mid.lastSeq}`);

  // 带 Last-Event-ID 续传
  const part2 = await readStream(r2.run_id, { lastSeq: mid.lastSeq });
  check(
    part2.length > 0 && part2[0]!.seq === mid.lastSeq + 1,
    "续传从断点的下一条开始，不重不漏",
    `期望 seq=${mid.lastSeq + 1}，实际 ${part2[0]?.seq}`,
  );

  const resumed = reduceAll(part2, mid);
  const allSeqs = [...part1, ...part2].map((e) => e.seq);
  check(
    allSeqs.every((s, i) => (i === 0 ? s === 1 : s === allSeqs[i - 1]! + 1)),
    "拼接后 seq 仍然连续",
    `1..${allSeqs.at(-1)}`,
  );
  check(resumed.status === "succeeded", "续传后仍达终态", resumed.status);
  check(resumed.todos.length >= 2, "续传后计划完整", `${resumed.todos.length} 条`);
  check(
    resumed.files.some((f) => f.path === "/resume.txt"),
    "续传后文件完整",
  );

  // ---------- 场景 3：重放幂等 ----------
  console.log("\n场景 3 · 重复事件不污染状态（规则 2 去重）");
  const doubled = reduceAll([...part1, ...part1, ...part2], initialRunState);
  check(
    doubled.text === resumed.text && doubled.todos.length === resumed.todos.length,
    "同一批事件重放两遍，归约结果不变",
  );

  // ---------- 场景 4：lib/sse.ts 封装本身 ----------
  // 前面三个场景验的是协议和归约，这里验封装里最容易写错的部分：
  // 收到终态事件后必须主动 abort，否则库会把「服务端关闭」当异常而无限重连。
  console.log("\n场景 4 · streamRunEvents 封装");
  const t3 = await api<{ id: string }>("/v1/threads", {
    method: "POST",
    body: JSON.stringify({ agent_id: agent.id, title: "" }),
  });
  const r3 = await api<{ run_id: string }>(`/v1/threads/${t3.id}/runs`, {
    method: "POST",
    body: JSON.stringify({ content: [{ type: "text", text: "只回复 OK" }] }),
  });

  let wrapped: RunState = initialRunState;
  let closed = false;
  let opens = 0;
  let wrapError: Error | null = null;

  const started = Date.now();
  const ctrl = new AbortController();
  await streamRunEvents({
    runId: r3.run_id,
    signal: ctrl.signal,
    onOpen: () => opens++,
    onEvent: (e) => {
      wrapped = applyEvent(wrapped, e);
    },
    onClose: () => {
      closed = true;
    },
    onError: (e) => {
      wrapError = e;
    },
  });
  const elapsed = Date.now() - started;

  check(wrapError === null, "封装未报错", wrapError ? String(wrapError) : "");
  check(closed, "onClose 被调用（正常收线，非异常路径）");
  check(opens === 1, "只连接了一次，没有重连风暴", `onOpen ×${opens}`);
  check(wrapped.status === "succeeded", "封装归约出终态", wrapped.status);
  check(elapsed < 60_000, "终态后立即返回，未挂在重连里", `${(elapsed / 1000).toFixed(1)}s`);

  // ---------- 场景 5：断线续传走封装 ----------
  // 场景 2 验的是协议层。这里验封装是否真的把 lastSeq 变成了 Last-Event-ID ——
  // 这是"刷新页面不丢消息"的实现基础，写错了只会表现为消息重复，很隐蔽。
  console.log("\n场景 5 · streamRunEvents 断线续传");
  const t4 = await api<{ id: string }>("/v1/threads", {
    method: "POST",
    body: JSON.stringify({ agent_id: agent.id, title: "" }),
  });
  const r4 = await api<{ run_id: string }>(`/v1/threads/${t4.id}/runs`, {
    method: "POST",
    body: JSON.stringify({
      content: [{ type: "text", text: "把 3*4 的结果写进 /wrap.txt。先列两条待办。" }],
    }),
  });

  // 第一段：收满 3 条就 abort，模拟用户刷新页面
  let half: RunState = initialRunState;
  const ctrlA = new AbortController();
  await streamRunEvents({
    runId: r4.run_id,
    signal: ctrlA.signal,
    onEvent: (e) => {
      half = applyEvent(half, e);
      if (half.lastSeq >= 3) ctrlA.abort();
    },
  });
  check(half.lastSeq >= 3, "第一段收到至少 3 条后中断", `lastSeq=${half.lastSeq}`);

  // 第二段：带上 lastSeq 续传
  const seen: number[] = [];
  let full: RunState = half;
  const ctrlB = new AbortController();
  await streamRunEvents({
    runId: r4.run_id,
    lastSeq: half.lastSeq,
    signal: ctrlB.signal,
    onEvent: (e) => {
      seen.push(e.seq);
      full = applyEvent(full, e);
    },
  });

  check(
    seen.length > 0 && seen[0] === half.lastSeq + 1,
    "封装的续传从断点下一条开始",
    `期望 ${half.lastSeq + 1}，实际 ${seen[0]}`,
  );
  check(
    seen.every((s, i) => i === 0 || s === seen[i - 1]! + 1),
    "续传段内部无空洞",
  );
  check(full.status === "succeeded", "续传后达到终态", full.status);
  check(
    full.files.some((f) => f.path === "/wrap.txt") && full.todos.length >= 2,
    "跨断点的状态完整（文件 + 计划都在）",
  );

  console.log(`\n${failures === 0 ? "全部通过" : `${failures} 项失败`}`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error("验证脚本异常：", err);
  process.exit(1);
});
