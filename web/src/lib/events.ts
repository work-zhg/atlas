/**
 * TraceEvent 契约的前端镜像 —— 对齐 docs/backend-design.md §4.2。
 *
 * ★ 这份类型**生成不了**：TraceEvent.data 在 OpenAPI 里是自由 dict，
 *   openapi-typescript 只能给出 Record<string, unknown>。而 data 恰恰是
 *   前端渲染的全部依据，所以手写判别联合，让 tsc 帮忙兜住字段名拼错。
 *
 * 契约三条规则（破了就是 breaking change）：
 *   1. todos.updated 是**全量快照**，前端不做 diff 合并
 *   2. seq 在单个 run 内从 1 严格递增无空洞，靠它去重与补齐
 *   3. data 只增字段不删不改类型 —— 所以未知 type 必须静默忽略，不能抛错
 */

export const EventType = {
  RunStarted: "run.started",
  RunFinished: "run.finished",
  RunFailed: "run.failed",
  RunCancelled: "run.cancelled",
  ThinkingDelta: "thinking.delta",
  MessageDelta: "message.delta",
  MessageCompleted: "message.completed",
  TodosUpdated: "todos.updated",
  ToolStarted: "tool.started",
  ToolCompleted: "tool.completed",
  ToolFailed: "tool.failed",
  SubagentStarted: "subagent.started",
  SubagentStep: "subagent.step",
  SubagentFinished: "subagent.finished",
  FileWritten: "file.written",
  FileDeleted: "file.deleted",
  UsageUpdated: "usage.updated",
  ApprovalRequired: "approval.required",
  ContextCompacted: "context.compacted",
  TitleGenerated: "thread.title_generated",
} as const;

export type EventTypeValue = (typeof EventType)[keyof typeof EventType];

/** run 终态事件 —— 收到即关闭 SSE，不再重连。 */
export const TERMINAL_EVENTS: readonly string[] = [
  EventType.RunFinished,
  EventType.RunFailed,
  EventType.RunCancelled,
];

// ---------- data 载荷 ----------

export type TodoStatus = "pending" | "in_progress" | "completed";

export interface Todo {
  id?: string | number;
  content: string;
  status: TodoStatus;
}

export interface RunStartedData {
  agent_slug: string;
  agent_name: string;
  model: string;
  effort: string | null;
  thinking: string;
  tools: string[];
  /** 请求了但本期未接的工具。§13.2 不允许静默降级，必须显示出来。 */
  unsupported_tools?: string[];
}

export interface RunFinishedData {
  total_tokens: number;
  text_len: number;
}

export interface RunFailedData {
  error_kind: string;
  message: string;
  partial_text?: string;
  [k: string]: unknown;
}

export interface RunCancelledData {
  partial_text_len: number;
}

export interface MessageDeltaData {
  text: string;
  /**
   * 段号。模型调工具前说的话与拿到结果后的结论是**两轮发言**，后端按工具
   * 调用切段（server 的 domain/events.py::Answer）。没有它的话流式过程中
   * 只能把两轮糊成一条 —— 用户分不出哪句是结论。
   *
   * thinking.delta 复用同一个 data 形状，但不分段，所以是可选的。
   */
  block?: number;
}

export interface ContentBlock {
  type: string;
  text?: string;
  [k: string]: unknown;
}

export interface MessageCompletedData {
  content: ContentBlock[];
}

export interface TodosUpdatedData {
  todos: Todo[];
}

export interface ToolStartedData {
  call_id: string;
  name: string;
  args_preview?: string;
  args?: unknown;
}

export interface ToolCompletedData {
  call_id: string;
  duration_ms?: number;
  result_preview?: string;
  result?: unknown;
  status?: string;
}

export interface ToolFailedData {
  call_id: string;
  duration_ms?: number;
  error: string;
  error_kind?: string;
  fallback_applied?: boolean;
}

export interface FileWrittenData {
  path: string;
  size_bytes?: number;
  digest?: string;
}

export interface FileDeletedData {
  path: string;
}

export interface UsageUpdatedData {
  input_tokens?: number;
  output_tokens?: number;
  cache_read?: number;
  cache_write?: number;
  total_tokens?: number;
  thinking_occurred?: boolean;
}

export interface SubagentStartedData {
  subagent_run_id: string;
  name: string;
  task: string;
}

export interface SubagentStepData {
  subagent_run_id: string;
  index: number;
  summary: string;
}

export interface ContextCompactedData {
  messages_summarized: number;
  tokens_before: number;
  tokens_after: number;
  summary: string;
  degraded?: boolean;
}

export interface TitleGeneratedData {
  title: string;
  degraded?: boolean;
}

export interface ApprovalRequiredData {
  approval_id: string;
  tool_name: string;
  args: unknown;
  reason: string;
  /**
   * 决策要提交到哪个 run。**委派场景下不等于当前流的 run** ——
   * 子智能体的审批发生在子 run 上，由父 run 的流代为转发（server 的
   * services/subagent.py::_forward_approvals），提交时必须回到子 run 的
   * 端点。缺省 = 当前流的 run（native 的普通审批就是这种）。
   */
  run_id?: string;
}

// ---------- 事件信封 ----------

interface Envelope<T extends string, D> {
  seq: number;
  run_id: string;
  ts: string;
  /** 0=主 agent，1=子 agent。用于消息流缩进。 */
  depth: number;
  type: T;
  data: D;
}

export type TraceEvent =
  | Envelope<typeof EventType.RunStarted, RunStartedData>
  | Envelope<typeof EventType.RunFinished, RunFinishedData>
  | Envelope<typeof EventType.RunFailed, RunFailedData>
  | Envelope<typeof EventType.RunCancelled, RunCancelledData>
  | Envelope<typeof EventType.ThinkingDelta, MessageDeltaData>
  | Envelope<typeof EventType.MessageDelta, MessageDeltaData>
  | Envelope<typeof EventType.MessageCompleted, MessageCompletedData>
  | Envelope<typeof EventType.TodosUpdated, TodosUpdatedData>
  | Envelope<typeof EventType.ToolStarted, ToolStartedData>
  | Envelope<typeof EventType.ToolCompleted, ToolCompletedData>
  | Envelope<typeof EventType.ToolFailed, ToolFailedData>
  | Envelope<typeof EventType.SubagentStarted, SubagentStartedData>
  | Envelope<typeof EventType.SubagentStep, SubagentStepData>
  | Envelope<typeof EventType.SubagentFinished, SubagentStepData>
  | Envelope<typeof EventType.FileWritten, FileWrittenData>
  | Envelope<typeof EventType.FileDeleted, FileDeletedData>
  | Envelope<typeof EventType.UsageUpdated, UsageUpdatedData>
  | Envelope<typeof EventType.ApprovalRequired, ApprovalRequiredData>
  | Envelope<typeof EventType.ContextCompacted, ContextCompactedData>
  | Envelope<typeof EventType.TitleGenerated, TitleGeneratedData>;

/** 后端将来新增的事件类型（规则 3）—— 解析后但本前端还不认识的，走这里。 */
export type UnknownTraceEvent = Envelope<string, Record<string, unknown>>;

export type AnyTraceEvent = TraceEvent | UnknownTraceEvent;

/** 运行时校验：只认信封结构，不校验 data —— data 只增字段，严校验反而会误伤。 */
export function parseTraceEvent(raw: string): AnyTraceEvent | null {
  try {
    const v = JSON.parse(raw) as Partial<UnknownTraceEvent>;
    if (typeof v.seq !== "number" || typeof v.type !== "string") return null;
    return {
      seq: v.seq,
      run_id: String(v.run_id ?? ""),
      ts: String(v.ts ?? ""),
      depth: typeof v.depth === "number" ? v.depth : 0,
      type: v.type,
      data: (v.data ?? {}) as Record<string, unknown>,
    } as AnyTraceEvent;
  } catch {
    return null;
  }
}

export function isTerminalEvent(type: string): boolean {
  return TERMINAL_EVENTS.includes(type);
}
