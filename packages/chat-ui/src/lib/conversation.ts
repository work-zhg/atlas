/**
 * 对话怎么摆：落库的消息 + 实时区 → 一条条要画的东西。网页（chat/MessageStream.tsx）与手机 App 共用。
 *
 * 只做决定、不画：哪几条消息要画、一轮挂起续跑后落下的几条怎么合成一块、实时区什么时候让位给落库的
 * 消息、时间线里连续的工具调用怎么归成一组「步骤」。规则一处写，两端不会各画各的。
 */
import type { Message } from "../api/types";
import { isStreamDone, orphanSubRuns, type RunState, type SubagentRun, type TimelineItem, type ToolCall } from "./run-reducer";

/**
 * 正文分段。一个 run 里模型可能说好几轮 —— 调工具前一段、拿到结果后一段，
 * 后端按轮切成多个 text block（server 的 domain/events.py::Answer）。
 * 这里不 join：最后一段是结论，前面的是过程，要分开渲染。
 */
export function blocksOf(content: Message["content"]): string[] {
  return content
    .filter((b) => b.type === "text" && typeof b.text === "string")
    .map((b) => b.text as string)
    .filter((t) => t.length > 0);
}

export function textOf(content: Message["content"]): string {
  return blocksOf(content).join("");
}

export type ConversationEntry =
  | { kind: "user"; message: Message; text: string }
  | {
      kind: "assistant";
      /** 承载这一轮的那条（这一轮的第一条） */
      message: Message;
      /** 这一轮所有助手消息的正文段（挂起续跑的一轮会落下好几条，合成一块显示） */
      blocks: string[];
      /** 反馈指向这一轮的最后一条（结论所在） */
      feedbackMessageId: string;
      /** 要不要取这一轮的归档轨迹（GET /runs/{id}/trace） */
      traceEnabled: boolean;
      /** 刚结束的这一轮：轨迹取回之前先用实时状态顶上，否则过程会先消失、再闪回来 */
      useLiveFallback: boolean;
      /** 最后一条回答、且实时区不在显示：可以「重新生成」 */
      canRegenerate: boolean;
    };

export interface ConversationPlan {
  entries: ConversationEntry[];
  /** 实时区要不要画 */
  showLive: boolean;
  /** 实时的这一轮已经结束（含失败、取消） */
  turnOver: boolean;
}

export function planConversation(
  messages: Message[],
  live: RunState | undefined,
  liveRunId: string | undefined,
): ConversationPlan {
  // 助手消息已落库后就不再渲染实时块，避免同一段话出现两遍
  const persisted = liveRunId ? messages.some((m) => m.run_id === liveRunId && m.role === "assistant") : false;
  // ★ 只有这一轮**真正结束**后，落库的消息才算覆盖了实时块。
  //
  //   挂起（suspended）不算结束：那一段的助手消息确实已经落库了，但这一轮
  //   还在等子智能体 —— 只看 persisted 的话「正在等待子智能体」这条提示会
  //   在挂起的瞬间消失，用户看到的是一个静止且无解释的界面。
  const turnOver = live ? isStreamDone(live.status) : false;
  const showLive = Boolean(live) && live!.status !== "idle" && !(persisted && turnOver);
  // ★ 实时区在显示时，这一轮已落库的助手消息（挂起时落下的前一段）就不再单独渲染，
  //   否则同一段话出现两遍。只在实时区**确实包含**那些段落时才隐藏 —— 刷新后
  //   首连只回放最近一段事件，实时区可能没收到前一段，那时要靠落库的消息补上。
  const liveBlocks = new Set(showLive && live ? live.blocks.filter((b) => b.length > 0) : []);
  const coveredByLive = (m: Message): boolean => {
    if (!showLive || m.role !== "assistant" || m.run_id !== liveRunId) return false;
    const blocks = blocksOf(m.content);
    return blocks.length > 0 && blocks.every((b) => liveBlocks.has(b));
  };
  const shown = messages.filter((m) => !coveredByLive(m));
  // ★ 一轮只画一块：挂起续跑的一轮会落下好几条助手消息（每段一条），过程在轨迹里本来就是一整段。
  //   第一条承载整轮（正文段合并），后面几条不再单独画；反馈指向最后一条（结论所在）。
  const runOf = (m: Message) => m.run_id ?? m.id;
  const firstOfRun = new Map<string, string>();
  const lastOfRun = new Map<string, string>();
  const runBlocks = new Map<string, string[]>();
  for (const m of shown) {
    if (m.role !== "assistant") continue;
    const run = runOf(m);
    if (!firstOfRun.has(run)) firstOfRun.set(run, m.id);
    lastOfRun.set(run, m.id);
    runBlocks.set(run, [...(runBlocks.get(run) ?? []), ...blocksOf(m.content)]);
  }
  const lastAnswer = [...shown].reverse().find((m) => m.role === "assistant");
  // ★ 分页边界可能把一轮切成两条消息：轨迹只挂在第一条上，避免整段过程出现两遍
  const tracedRuns = new Set<string>();

  const entries: ConversationEntry[] = [];
  for (const m of shown) {
    if (m.role === "user") {
      entries.push({ kind: "user", message: m, text: textOf(m.content) });
      continue;
    }
    const run = runOf(m);
    if (firstOfRun.get(run) !== m.id) continue;
    let traceEnabled = false;
    // 进行中（含挂起）的那一轮走实时区，不取轨迹 —— 否则会缓存一份残缺的
    if (m.run_id && !(m.run_id === liveRunId && !turnOver) && !tracedRuns.has(m.run_id)) {
      tracedRuns.add(m.run_id);
      traceEnabled = true;
    }
    entries.push({
      kind: "assistant",
      message: m,
      blocks: runBlocks.get(run) ?? [],
      feedbackMessageId: lastOfRun.get(run) ?? m.id,
      traceEnabled,
      useLiveFallback: Boolean(m.run_id && m.run_id === liveRunId && turnOver),
      canRegenerate: Boolean(lastAnswer && runOf(lastAnswer) === run && !showLive),
    });
  }
  return { entries, showLive, turnOver };
}

export type TimelineEntry =
  | { kind: "steps"; key: string; calls: ToolCall[] }
  | { kind: "item"; key: string; item: TimelineItem; isLast: boolean }
  /** 找不到所属卡片的子 run（见 orphanSubRuns），排在主轮次末尾 */
  | { kind: "orphan"; key: string; run: SubagentRun; sub: RunState };

const ORPHAN_STATUS: Record<RunState["status"], SubagentRun["status"]> = {
  idle: "running",
  running: "running",
  suspended: "running",
  succeeded: "ok",
  failed: "error",
  cancelled: "error",
};

/**
 * 一个 run 的过程时间线要画的东西。实时区与历史轮次共用，子 run 的卡片里也是它。
 * root 缺省 = state，即主轮次。
 */
export function timelineEntries(state: RunState, root: RunState = state): TimelineEntry[] {
  const last = state.timeline.length - 1;
  const out: TimelineEntry[] = [];
  // ★ 连续的工具调用归成一组「步骤」：中间夹了正文、委派、审批就另起一组 —— 先后顺序不能乱
  let group: ToolCall[] = [];
  const flush = () => {
    if (group.length) out.push({ kind: "steps", key: `steps:${group[0]!.callId}`, calls: group });
    group = [];
  };
  state.timeline.forEach((item, i) => {
    if (item.kind === "tool") {
      // 向用户提问的那次调用由问题卡片完整呈现，不算一个步骤（按 tool_call_id 关联）
      if (state.approvals.some((a) => a.kind === "question" && a.toolCallId === item.callId)) return;
      const call = state.toolCalls.find((c) => c.callId === item.callId);
      if (call) group.push(call);
      return;
    }
    flush();
    out.push({ kind: "item", key: itemKey(item), item, isLast: i === last });
  });
  flush();

  if (state === root) {
    for (const id of orphanSubRuns(root)) {
      const sub = root.subRuns[id]!;
      const run: SubagentRun = {
        runId: id,
        name: sub.meta?.agent_name || "子智能体",
        task: "",
        status: ORPHAN_STATUS[sub.status],
      };
      out.push({ kind: "orphan", key: `orphan:${id}`, run, sub });
    }
  }
  return out;
}

export function itemKey(item: TimelineItem): string {
  switch (item.kind) {
    case "text":
      return `text:${item.block}`;
    case "tool":
      return `tool:${item.callId}`;
    case "subagent":
      return `subagent:${item.runId}`;
    case "approval":
      return `approval:${item.approvalId}`;
    case "compaction":
      return `compaction:${item.seq}`;
    case "mode":
      return `mode:${item.runId}`;
    case "notice":
      return `notice:${item.seq}`;
    case "todos":
      return "todos";
  }
}
