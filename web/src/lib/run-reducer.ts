/**
 * TraceEvent 流 → RunState 的归约。
 *
 * 刻意做成**纯函数**：不碰 React、不碰网络，于是可以直接喂一串事件做单测，
 * 也能在断线续传后把补收的事件重放一遍而不出现重复。
 *
 * 契约三条规则在这里落实：
 *   1. todos.updated 整表替换（write_todos 就是替换语义）
 *   2. 按 seq 去重 —— 重连时后端可能把边界那条重发一次
 *   3. 未知 type 静默忽略 —— 后端加新事件不该让前端崩
 */
import {
  EventType,
  type AnyTraceEvent,
  type ContentBlock,
  type ContextCompactedData,
  type FileWrittenData,
  type MessageCompletedData,
  type MessageDeltaData,
  type RunFailedData,
  type ApprovalRequiredData,
  type RunStartedData,
  type SubagentStartedData,
  type Todo,
  type TodosUpdatedData,
  type ToolCompletedData,
  type ToolFailedData,
  type ToolStartedData,
  type UsageUpdatedData,
} from "./events";

export type StreamStatus = "idle" | "running" | "succeeded" | "failed" | "cancelled";

export interface ToolCall {
  callId: string;
  name: string;
  argsPreview?: string;
  args?: unknown;
  status: "running" | "ok" | "error";
  durationMs?: number;
  resultPreview?: string;
  result?: unknown;
  error?: string;
  errorKind?: string;
  fallbackApplied?: boolean;
  /** 子 agent 的调用缩进用 */
  depth: number;
}

export interface FileEntry {
  path: string;
  sizeBytes?: number;
  digest?: string;
  deleted?: boolean;
}

export interface Usage {
  inputTokens?: number;
  outputTokens?: number;
  cacheRead?: number;
  cacheWrite?: number;
  totalTokens?: number;
  thinkingOccurred?: boolean;
}

export interface SubagentRun {
  /** = 主 agent 那次 task 调用的 tool_call_id，started/finished 靠它配对 */
  runId: string;
  name: string;
  task: string;
  status: "running" | "ok" | "error";
  result?: string;
  durationMs?: number;
}

export interface PendingApproval {
  approvalId: string;
  toolName: string;
  args: unknown;
  reason: string;
  /** 提交决策的目标 run。委派来的审批属于子 run，与当前流的 run 不同。 */
  runId?: string;
}

export interface CompactionMark {
  seq: number;
  messagesSummarized: number;
  tokensBefore: number;
  tokensAfter: number;
  summary: string;
  degraded: boolean;
}

export interface RunState {
  status: StreamStatus;
  /** 流式正文（message.delta 累加；message.completed 到达后以其为准） */
  text: string;
  /**
   * 正文分段。模型调工具前说的话与最终结论是两轮发言，后端按工具调用
   * 切段并在 message.delta 上带 block 序号 —— 渲染时要把「过程」与
   * 「结论」区分开，不能拼成一条。
   *
   * text 保留为**全文**（= blocks.join("")），给不关心分段的地方用。
   */
  blocks: string[];
  /** 全量快照，不做 diff 合并（规则 1） */
  todos: Todo[];
  /** 按 call_id 索引；用数组保序，Map 在 React 里比较麻烦 */
  toolCalls: ToolCall[];
  files: FileEntry[];
  /** 委派出去的子智能体（P5）。内部工具调用在 toolCalls 里带 depth≥1。 */
  subagents: SubagentRun[];
  usage: Usage;
  /** 已处理的最大 seq —— 断线续传的游标 */
  lastSeq: number;
  meta?: RunStartedData;
  error?: { kind: string; message: string; partialText?: string };
  compactions: CompactionMark[];
  /** 待人工确认的工具调用（§12.2）。决策提交后由调用方清空。 */
  pendingApprovals: PendingApproval[];
  /** 后端生成的会话标题（§8），到达后左侧列表要更新 */
  generatedTitle?: string;
  /** 最终 content blocks，markdown 渲染以它为准 */
  content?: ContentBlock[];
}

export const initialRunState: RunState = {
  status: "idle",
  text: "",
  blocks: [],
  todos: [],
  toolCalls: [],
  files: [],
  subagents: [],
  usage: {},
  lastSeq: 0,
  compactions: [],
  pendingApprovals: [],
};

function upsertSubagent(
  list: SubagentRun[],
  runId: string,
  patch: Partial<SubagentRun>,
): SubagentRun[] {
  const i = list.findIndex((s) => s.runId === runId);
  if (i === -1) {
    return [...list, { runId, name: "子智能体", task: "", status: "running", ...patch }];
  }
  const next = [...list];
  next[i] = { ...next[i]!, ...patch };
  return next;
}

function upsertTool(list: ToolCall[], callId: string, patch: Partial<ToolCall>): ToolCall[] {
  const i = list.findIndex((t) => t.callId === callId);
  if (i === -1) {
    // tool.completed 先于 tool.started 到达在理论上不该发生，但真发生了
    // 也不能丢数据 —— 补一条占位，总比工具页少一行强。
    return [
      ...list,
      { callId, name: patch.name ?? "(未知工具)", status: "running", depth: 0, ...patch },
    ];
  }
  const next = [...list];
  next[i] = { ...next[i]!, ...patch };
  return next;
}

function upsertFile(list: FileEntry[], entry: FileEntry): FileEntry[] {
  const i = list.findIndex((f) => f.path === entry.path);
  if (i === -1) return [...list, entry];
  const next = [...list];
  next[i] = { ...next[i]!, ...entry };
  return next;
}

export function applyEvent(state: RunState, event: AnyTraceEvent): RunState {
  // 规则 2：seq 严格递增。重连边界可能重发，旧的直接丢弃。
  if (event.seq <= state.lastSeq) return state;

  const base = { ...state, lastSeq: event.seq };
  const d = event.data as Record<string, unknown>;

  switch (event.type) {
    case EventType.RunStarted:
      return { ...base, status: "running", meta: d as unknown as RunStartedData };

    case EventType.MessageDelta: {
      const md = d as unknown as MessageDeltaData;
      const delta = md.text ?? "";
      // ★ block 缺省当 0：旧后端（不发 block）退化成单段，行为与从前一致
      const at = md.block ?? 0;
      const blocks = [...base.blocks];
      while (blocks.length <= at) blocks.push("");
      blocks[at] += delta;
      return { ...base, text: base.text + delta, blocks };
    }

    case EventType.MessageCompleted: {
      // 以最终内容为准：中途漏收一条 delta 也能自愈
      const content = (d as unknown as MessageCompletedData).content ?? [];
      const parts = content
        .filter((b) => b.type === "text" && typeof b.text === "string")
        .map((b) => b.text as string);
      const text = parts.join("");
      return { ...base, content, text: text || base.text, blocks: parts.length ? parts : base.blocks };
    }

    case EventType.TodosUpdated:
      // 规则 1：整表替换
      return { ...base, todos: (d as unknown as TodosUpdatedData).todos ?? [] };

    case EventType.ToolStarted: {
      const t = d as unknown as ToolStartedData;
      return {
        ...base,
        toolCalls: upsertTool(base.toolCalls, t.call_id, {
          callId: t.call_id,
          name: t.name,
          argsPreview: t.args_preview,
          args: t.args,
          status: "running",
          depth: event.depth,
        }),
      };
    }

    case EventType.ToolCompleted: {
      const t = d as unknown as ToolCompletedData;
      return {
        ...base,
        toolCalls: upsertTool(base.toolCalls, t.call_id, {
          status: "ok",
          durationMs: t.duration_ms,
          resultPreview: t.result_preview,
          result: t.result,
        }),
      };
    }

    case EventType.ToolFailed: {
      const t = d as unknown as ToolFailedData;
      return {
        ...base,
        toolCalls: upsertTool(base.toolCalls, t.call_id, {
          status: "error",
          durationMs: t.duration_ms,
          error: t.error,
          errorKind: t.error_kind,
          fallbackApplied: t.fallback_applied,
        }),
      };
    }

    case EventType.SubagentStarted: {
      const s = d as unknown as SubagentStartedData;
      return {
        ...base,
        subagents: upsertSubagent(base.subagents, s.subagent_run_id, {
          runId: s.subagent_run_id,
          name: s.name || "子智能体",
          task: s.task ?? "",
          status: "running",
        }),
      };
    }

    case EventType.SubagentFinished: {
      const runId = String(d.subagent_run_id ?? "");
      const failed = Boolean(d.failed) || d.status === "error";
      return {
        ...base,
        subagents: upsertSubagent(base.subagents, runId, {
          status: failed ? "error" : "ok",
          result: typeof d.result === "string" ? d.result : undefined,
          durationMs: typeof d.duration_ms === "number" ? d.duration_ms : undefined,
        }),
      };
    }

    case EventType.FileWritten: {
      const f = d as unknown as FileWrittenData;
      return {
        ...base,
        files: upsertFile(base.files, {
          path: f.path,
          sizeBytes: f.size_bytes,
          digest: f.digest,
          deleted: false,
        }),
      };
    }

    case EventType.FileDeleted:
      return {
        ...base,
        files: upsertFile(base.files, { path: String(d.path ?? ""), deleted: true }),
      };

    case EventType.UsageUpdated: {
      const u = d as unknown as UsageUpdatedData;
      return {
        ...base,
        usage: {
          inputTokens: u.input_tokens,
          outputTokens: u.output_tokens,
          cacheRead: u.cache_read,
          cacheWrite: u.cache_write,
          totalTokens: u.total_tokens,
          thinkingOccurred: u.thinking_occurred,
        },
      };
    }

    case EventType.ContextCompacted: {
      const c = d as unknown as ContextCompactedData;
      return {
        ...base,
        compactions: [
          ...base.compactions,
          {
            seq: event.seq,
            messagesSummarized: c.messages_summarized,
            tokensBefore: c.tokens_before,
            tokensAfter: c.tokens_after,
            summary: c.summary,
            degraded: Boolean(c.degraded),
          },
        ],
      };
    }

    case EventType.ApprovalRequired: {
      const a = d as unknown as ApprovalRequiredData;
      return {
        ...base,
        pendingApprovals: [
          ...base.pendingApprovals,
          {
            approvalId: a.approval_id,
            toolName: a.tool_name,
            args: a.args,
            reason: a.reason ?? "",
            runId: a.run_id,
          },
        ],
      };
    }

    case EventType.TitleGenerated:
      return { ...base, generatedTitle: String(d.title ?? "") };

    case EventType.RunFinished:
      return { ...base, status: "succeeded" };

    case EventType.RunFailed: {
      const f = d as unknown as RunFailedData;
      return {
        ...base,
        status: "failed",
        error: {
          kind: f.error_kind ?? "unknown",
          message: f.message ?? "运行失败",
          partialText: f.partial_text,
        },
      };
    }

    case EventType.RunCancelled:
      return { ...base, status: "cancelled" };

    default:
      // 规则 3：后端新增的事件类型（subagent.* / approval.* 等本期未渲染的）
      // 走到这里。只推进 lastSeq，不做别的 —— 绝不抛错。
      return base;
  }
}

export function applyEvents(state: RunState, events: AnyTraceEvent[]): RunState {
  return events.reduce(applyEvent, state);
}

/** 流是否已结束 —— 决定要不要显示光标、能不能点取消。 */
export function isStreamDone(status: StreamStatus): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}
