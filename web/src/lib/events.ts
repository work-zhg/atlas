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
  /**
   * 这一段跑完了，但这一轮**没结束** —— 在等子智能体的结果（可能等一小时）。
   *
   * ★ 不是终态事件：SSE 不关闭，后面还会来这一轮真正的结束。放进
   *   TERMINAL_EVENTS 的话流会在委派刚开始时断掉，用户永远等不到结论。
   */
  RunSuspended: "run.suspended",
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
  /** acp：这一轮 CLI 实际生效的权限模式（可能被降级，必须让用户看见） */
  AgentMode: "agent.mode",
  ContextCompacted: "context.compacted",
  TitleGenerated: "thread.title_generated",
  /** 模型读了某个技能的 SKILL.md —— 「选中了这个技能」唯一可观测的信号 */
  SkillLoaded: "skill.loaded",
  /** 引用的技能这一轮没装上（紧急下架等）。必须让用户看见 */
  SkillSkipped: "skill.skipped",
  /** MCP 工具定义与保存时不一致，或尚未复核 */
  McpToolDrift: "mcp.tool_drift",
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

export interface SuspensionWait {
  /** 等子智能体的结论，还是等人点头。 */
  reason: "delegation" | "approval";
  /** delegation → 子 run 的 id；approval → approval 的 id。 */
  token: string;
}

export interface RunSuspendedData {
  /**
   * 这一轮在等什么。
   *
   * ★ 带 reason 是必须的：UI 要区分「在等机器」和「在等你」—— 前者用户只能
   *   干等，后者需要他行动（去点那个弹窗）。
   */
  waiting_on: SuspensionWait[];
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
  /** "auto_mode_denied" = 被 CLI 的 auto 模式判定为有风险而拦下（不是工具坏了） */
  error_kind?: string;
  fallback_applied?: boolean;
  /** auto 模式的拦截类别，如 "Unverifiable Deletion Target" */
  denied_reason?: string;
  result?: unknown;
  result_preview?: string;
}

/** 平台的权限模式取值；不认识的（未来新增的）原样透传 */
export type PermissionMode = "manual" | "accept_edits" | "auto" | "plan" | (string & {});

export interface AgentModeData {
  requested: PermissionMode | null;
  /** null = CLI 不支持权限模式 */
  effective: PermissionMode | null;
  degraded: boolean;
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
  /** run 内单调递增。会话流下只剩诊断价值 —— 游标是 thread_seq。 */
  seq: number;
  /**
   * 会话内单调递增 —— **会话流的游标**，去重与续传都以它为准。
   *
   * ★ 两个序号不能混用。数值相近（都是小整数），混了不报错，只是重连时
   *   游标被拿到另一个体系里比较，补发范围整个错位。
   */
  thread_seq: number;
  run_id: string;
  ts: string;
  /**
   * = run 在委派树里的深度：0 主 agent，1 子智能体。
   *
   * ★ 会话流里两者混在一条流上，靠它路由：depth≥1 的事件不能进主轮次的
   *   状态（子 run 的 run.finished 会把主轮次标成结束，它的 delta 会让
   *   子智能体的话冒进主对话）。见 run-reducer 的 applyEvent。
   */
  depth: number;
  type: T;
  data: D;
}

export interface SkillLoadedData {
  slug: string;
  version: number;
  call_id?: string;
}

export interface SkillSkippedData {
  slug: string;
  version: number;
  /** revoked = 紧急下架 */
  reason: string;
}

export interface McpToolDriftData {
  /** spec 标识 mcp:server:tool；整台 server 被拦时是 mcp:server:* */
  tool: string;
  server: string;
  old_digest?: string | null;
  new_digest?: string;
  /** used = 提示后照常用；blocked = 本轮不装；pending_review / rejected = 复核闸门 */
  action: "used" | "blocked" | "pending_review" | "rejected";
}

export type TraceEvent =
  | Envelope<typeof EventType.RunStarted, RunStartedData>
  | Envelope<typeof EventType.RunFinished, RunFinishedData>
  | Envelope<typeof EventType.RunFailed, RunFailedData>
  | Envelope<typeof EventType.RunCancelled, RunCancelledData>
  | Envelope<typeof EventType.RunSuspended, RunSuspendedData>
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
  | Envelope<typeof EventType.AgentMode, AgentModeData>
  | Envelope<typeof EventType.ContextCompacted, ContextCompactedData>
  | Envelope<typeof EventType.TitleGenerated, TitleGeneratedData>
  | Envelope<typeof EventType.SkillLoaded, SkillLoadedData>
  | Envelope<typeof EventType.SkillSkipped, SkillSkippedData>
  | Envelope<typeof EventType.McpToolDrift, McpToolDriftData>;

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
      thread_seq: typeof v.thread_seq === "number" ? v.thread_seq : 0,
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
