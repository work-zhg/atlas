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
  type AgentModeData,
  type AnyTraceEvent,
  type ContentBlock,
  type ContextCompactedData,
  type FileWrittenData,
  type McpToolDriftData,
  type SkillLoadedData,
  type SkillSkippedData,
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

/**
 * ★ "suspended" 是**未结束**态：这一轮在等子智能体的结果，之后还会接着跑。
 *   isStreamDone 因此不认它 —— 光标继续转、取消按钮继续可点，都是对的。
 */
export type StreamStatus =
  | "idle"
  | "running"
  | "suspended"
  | "succeeded"
  | "failed"
  | "cancelled";

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
  /** errorKind = "auto_mode_denied" 时：auto 模式的拦截类别 */
  deniedReason?: string;
  fallbackApplied?: boolean;
  /**
   * = run 在委派树里的深度：0 主 agent，1 子智能体。缩进用。
   *
   * ★ 语义变过一次：原先量的是 LangGraph 子图的命名空间深度，而委派改成
   *   「独立子 run」之后图内不再编译子图，那个口径恒为 0。现在它由后端按
   *   run 的父子关系给出（server 的 EventFactory.base_depth）。
   */
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

/**
 * 一轮里各样东西的**出现顺序**。只存引用，内容仍在各自的字段里。
 *
 * ★ 渲染必须按它走，而不是按类型分组：模型先说了一段话、再发起委派，界面上
 *   就该先是那段话、再是委派卡片。按类型分组（先画全部卡片、再画全部正文）
 *   会把先说的话挤到后面去，用户看到的因果是反的。
 */
export type TimelineItem =
  | { kind: "text"; block: number }
  | { kind: "tool"; callId: string }
  | { kind: "subagent"; runId: string }
  | { kind: "approval"; approvalId: string }
  | { kind: "todos" }
  | { kind: "compaction"; seq: number }
  | { kind: "mode"; runId: string }
  | { kind: "notice"; seq: number };

/**
 * 一行式的提示：技能被加载 / 被跳过、MCP 工具定义变了。它们不是工具调用，
 * 但要出现在发生的位置 —— 「模型先读了技能、再动手」本身就是可读的因果。
 */
export type Notice =
  | { seq: number; type: "skill.loaded"; data: SkillLoadedData }
  | { seq: number; type: "skill.skipped"; data: SkillSkippedData }
  | { seq: number; type: "mcp.tool_drift"; data: McpToolDriftData };

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
  /**
   * 委派出去的子智能体（P5）。
   *
   * ★ 子智能体的**内部过程**目前不在这条流上 —— 它跑在自己的子 run 里，
   *   有自己的事件流（doc/detail/subagent.html）。这里只有边界：
   *   subagent.started 给任务书，subagent.finished 给结论。
   *
   *   `ToolCall.depth≥1` 要等 thread 级事件流上线才会真的出现值
   *   （doc/detail/suspension.html §04/§05）。在那之前它恒为 0。
   */
  subagents: SubagentRun[];
  usage: Usage;
  /**
   * 子智能体的实时正文，按**子 run 的 run_id** 聚合。
   *
   * ★ 刻意与主对话分开：混进去的话用户会看到一段语气都不一样的话突然冒出来。
   *   渲染进对应的 SubAgentCard 还需要 tool_call_id ↔ sub_run_id 的关联
   *   （`SubagentRun.runId` 现在存的是 tool_call_id），那一步随 S6 一起做。
   */
  subRunText: Record<string, string>;
  /** 当前这一轮的 run —— 取消按钮、GET /runs/{id} 都用它。 */
  activeRunId?: string;
  /** 已处理的最大 thread_seq —— **会话级**游标，断线续传用 */
  lastSeq: number;
  meta?: RunStartedData;
  error?: { kind: string; message: string; partialText?: string };
  compactions: CompactionMark[];
  /** 技能 / MCP 的一行式提示（按 thread_seq 标识，跨 run 唯一） */
  notices: Notice[];
  /** 还在等人决定的审批（§12.2）。run 走到终态时出列。 */
  pendingApprovals: PendingApproval[];
  /**
   * 这一轮出现过的**全部**审批，不出列 —— 对话里的审批卡片要一直留在原位，
   * 处理完显示结果，而不是消失。
   */
  approvals: PendingApproval[];
  /** 出现顺序，渲染以它为准 */
  timeline: TimelineItem[];
  /** acp：每个 run（主 run 或子 run）实际生效的权限模式 */
  modes: Record<string, AgentModeData>;
  /**
   * 本段正文的起始段号。一轮挂起后续跑是新的一段，后端的段号从 0 重新数；
   * 加上这个偏移，续跑的话接在前一段后面，而不是覆盖它。
   */
  blockOffset: number;
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
  subRunText: {},
  lastSeq: 0,
  compactions: [],
  notices: [],
  pendingApprovals: [],
  approvals: [],
  timeline: [],
  modes: {},
  blockOffset: 0,
};

function sameItem(a: TimelineItem, b: TimelineItem): boolean {
  if (a.kind !== b.kind) return false;
  switch (a.kind) {
    case "text":
      return a.block === (b as typeof a).block;
    case "tool":
      return a.callId === (b as typeof a).callId;
    case "subagent":
      return a.runId === (b as typeof a).runId;
    case "approval":
      return a.approvalId === (b as typeof a).approvalId;
    case "compaction":
      return a.seq === (b as typeof a).seq;
    case "mode":
      return a.runId === (b as typeof a).runId;
    case "notice":
      return a.seq === (b as typeof a).seq;
    case "todos":
      return true;
  }
}

/** 一个 run 的权限模式：记下，并在它第一次出现的位置占位（后续回报只更新内容）。 */
function withMode(base: RunState, runId: string, mode: AgentModeData): RunState {
  return {
    ...base,
    modes: { ...base.modes, [runId]: mode },
    timeline: appendOnce(base.timeline, { kind: "mode", runId }),
  };
}

/** 第一次出现时排进时间线；之后的更新只改内容，不改位置。 */
function appendOnce(timeline: TimelineItem[], item: TimelineItem): TimelineItem[] {
  return timeline.some((t) => sameItem(t, item)) ? timeline : [...timeline, item];
}

/**
 * 记下一条审批：进「全部」与「待决定」两张表，并在时间线上占位。
 * ★ 同一个 approval_id 只记一次 —— 断线重连后可能再收到一遍。
 */
function withApproval(base: RunState, approval: PendingApproval): RunState {
  if (base.approvals.some((a) => a.approvalId === approval.approvalId)) return base;
  return {
    ...base,
    approvals: [...base.approvals, approval],
    pendingApprovals: [...base.pendingApprovals, approval],
    timeline: appendOnce(base.timeline, { kind: "approval", approvalId: approval.approvalId }),
  };
}

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

/**
 * 丢掉值为 undefined 的键。
 *
 * ★ 「这个键没给」和「这个键是空」在合并语义里不是一回事。事件流只带**变化
 *   的**字段（ACP 的 tool_call_update 尤其如此：title 在 start 时给过就不再
 *   重复，被拒的工具没有 output），而 `{...已有, ...patch}` 会让一个缺席的键
 *   变成 undefined，把先前记下的值原地抹掉。
 *
 *   真机上的表现：平台先发一条带原因的 tool.failed，CLI 随后发它自己那条
 *   （没有 output），于是失败原因在到达用户眼前之前就被覆盖没了。
 */
function definedOnly<T extends object>(patch: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(patch).filter(([, v]) => v !== undefined),
  ) as Partial<T>;
}

function upsertTool(list: ToolCall[], callId: string, patch: Partial<ToolCall>): ToolCall[] {
  const clean = definedOnly(patch);
  const i = list.findIndex((t) => t.callId === callId);
  if (i === -1) {
    // tool.completed 先于 tool.started 到达在理论上不该发生，但真发生了
    // 也不能丢数据 —— 补一条占位，总比工具页少一行强。
    return [
      ...list,
      { callId, name: patch.name ?? "(未知工具)", status: "running", depth: 0, ...clean },
    ];
  }
  const next = [...list];
  next[i] = { ...next[i]!, ...clean };
  return next;
}

/**
 * 一个 run 走到终态时，它名下的待审批一并出列。
 *
 * ★ 终态的 run 不可能还有审批在等 —— 审批属于 run，run 结束了它要么已被决策、
 *   要么已超时。此前 pendingApprovals 只增不减，「处理过」只记在组件内存里：
 *   刷新页面后首连回放把 approval.required 又加回来，弹窗就再弹一次。
 * ★ 只认终态，不认 run.suspended —— native 的审批正是靠挂起来等的。
 */
function withoutApprovalsOf(list: PendingApproval[], runId: string): PendingApproval[] {
  return list.filter((a) => a.runId !== runId);
}

/**
 * 子智能体的事件（depth≥1）。只取对用户有意义的四类。
 *
 * ★ approval.required 是这里最要紧的一条 —— 它是「委派挂起期间审批弹窗
 *   消失」这个问题的解法本身（doc/detail/suspension.html §06）。子智能体
 *   请求权限时，事件带着子 run 的 run_id 直接到前端，ApprovalCard 据此
 *   提交到正确的端点，不再依赖父 run 代为转发。
 */
function applySubEvent(
  base: RunState,
  event: AnyTraceEvent,
  d: Record<string, unknown>,
): RunState {
  switch (event.type) {
    case EventType.ApprovalRequired: {
      const a = d as unknown as ApprovalRequiredData;
      return withApproval(base, {
        approvalId: a.approval_id,
        toolName: a.tool_name,
        args: a.args,
        reason: a.reason ?? "",
        // ★ 会话流里每个事件都带 run_id，不需要 data 里再给一份 ——
        //   决策必须 POST 到**子** run 的端点。
        runId: a.run_id ?? event.run_id,
      });
    }

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
          depth: event.depth, // Inspector 据此缩进
        }),
        timeline: appendOnce(base.timeline, { kind: "tool", callId: t.call_id }),
      };
    }

    case EventType.ToolCompleted:
    case EventType.ToolFailed: {
      const t = d as unknown as ToolCompletedData & Partial<ToolFailedData>;
      return {
        ...base,
        toolCalls: upsertTool(base.toolCalls, t.call_id, {
          status: event.type === EventType.ToolFailed ? "error" : "ok",
          durationMs: t.duration_ms,
          resultPreview: t.result_preview,
          result: t.result,
          error: t.error,
          errorKind: t.error_kind,
          deniedReason: t.denied_reason,
        }),
      };
    }

    case EventType.AgentMode:
      // 子智能体（acp）这一轮的权限模式：在它出现的位置显示
      return withMode(base, event.run_id, d as unknown as AgentModeData);

    case EventType.MessageDelta: {
      // 子智能体的正文按子 run 聚合，**不进**主对话。
      // 渲染进具体卡片要等 tool_call_id ↔ sub_run_id 的关联（S6）——
      // 在那之前它只是被收着，不会污染任何东西。
      const md = d as unknown as MessageDeltaData;
      const prev = base.subRunText[event.run_id] ?? "";
      return {
        ...base,
        subRunText: { ...base.subRunText, [event.run_id]: prev + (md.text ?? "") },
      };
    }

    case EventType.RunFinished:
    case EventType.RunFailed:
    case EventType.RunCancelled:
      // 子 run 结束**不**改主轮次的状态（见 applyEvent 开头的隔离说明），
      // 只把它名下的审批出列。
      return { ...base, pendingApprovals: withoutApprovalsOf(base.pendingApprovals, event.run_id) };

    default:
      // 子 run 的其余 run.*/usage/todos/file 等一概不进主状态。
      return base;
  }
}

/**
 * 主 run 到终态时，还标着 running 的委派一并收尾。
 *
 * ★ 委派挂起后续跑的路径上，后端不会再发一条 subagent.finished（挂起时发的那条
 *   带 suspended=true，只说明「不再占着进程等」）。不收尾的话卡片永远停在
 *   「运行中」—— 历史轮次里尤其刺眼。主 run 都结束了，它的委派不可能还在跑。
 */
function settleSubagents(list: SubagentRun[], status: SubagentRun["status"]): SubagentRun[] {
  return list.some((s) => s.status === "running")
    ? list.map((s) => (s.status === "running" ? { ...s, status } : s))
    : list;
}

function upsertFile(list: FileEntry[], entry: FileEntry): FileEntry[] {
  const i = list.findIndex((f) => f.path === entry.path);
  if (i === -1) return [...list, entry];
  const next = [...list];
  next[i] = { ...next[i]!, ...entry };
  return next;
}

/** 一轮开始时要清掉的东西 —— 会话流跨轮次，不清会把两轮的轨迹叠在一起。 */
function freshTurn(): Omit<RunState, "lastSeq" | "activeRunId"> {
  const { lastSeq: _l, activeRunId: _a, ...rest } = initialRunState;
  return rest;
}

export function applyEvent(state: RunState, event: AnyTraceEvent): RunState {
  // 规则 2：thread_seq 在会话内严格递增。重连边界可能重发，旧的直接丢弃。
  //
  // ★ 游标是 thread_seq 而不是 seq：会话流上并发的两个 run（父挂起 + 子在跑）
  //   各有自己的 seq 序列，拿 seq 去重会把后来的整段丢掉。
  if (event.thread_seq <= state.lastSeq) return state;

  const base = { ...state, lastSeq: event.thread_seq };
  const d = event.data as Record<string, unknown>;

  // ★ 子智能体的事件（depth≥1）只取三类，其余一律不进主轮次的状态。
  //
  //   不隔离的后果是两个具体的 bug：子 run 的 run.finished 会把主轮次标成
  //   已结束（取消按钮消失、消息列表提前刷新），它的 message.delta 会让
  //   子智能体的话直接冒进主对话气泡 —— 语气都不一样的一段话。
  if (event.depth > 0) return applySubEvent(base, event, d);

  switch (event.type) {
    case EventType.RunStarted:
      // ★ 同一个 run 再来一次 run.started = 挂起后续跑的新一段，不是新的一轮。
      //   清掉的话前一段的话、委派卡片、审批都会从眼前消失。新一段的正文接在
      //   后面（blockOffset），后端的段号在新的一段里从 0 重新数。
      if (event.run_id === base.activeRunId) {
        return {
          ...base,
          status: "running",
          meta: d as unknown as RunStartedData,
          blockOffset: base.blocks.length,
        };
      }
      // ★ 开新一轮：会话流是跨轮次的，上一轮的正文/工具/待办必须清掉。
      //   lastSeq 保留 —— 它是会话级游标，清了会导致重连时重放整条会话。
      return {
        ...freshTurn(),
        lastSeq: base.lastSeq,
        activeRunId: event.run_id,
        status: "running",
        meta: d as unknown as RunStartedData,
      };

    case EventType.MessageDelta: {
      const md = d as unknown as MessageDeltaData;
      const delta = md.text ?? "";
      // ★ block 缺省当 0：旧后端（不发 block）退化成单段，行为与从前一致
      const at = base.blockOffset + (md.block ?? 0);
      const blocks = [...base.blocks];
      while (blocks.length <= at) blocks.push("");
      blocks[at] += delta;
      return {
        ...base,
        text: base.text + delta,
        blocks,
        // 空 delta 不占位：一段话真正开口时才排进时间线
        timeline: delta ? appendOnce(base.timeline, { kind: "text", block: at }) : base.timeline,
      };
    }

    case EventType.MessageCompleted: {
      // 以最终内容为准：中途漏收一条 delta 也能自愈
      const content = (d as unknown as MessageCompletedData).content ?? [];
      const parts = content
        .filter((b) => b.type === "text" && typeof b.text === "string")
        .map((b) => b.text as string);
      if (parts.length === 0) return { ...base, content };
      // 只替换本段的正文：续跑时前一段的话还在 blocks 的前面
      const blocks = [...base.blocks.slice(0, base.blockOffset), ...parts];
      let timeline = base.timeline;
      blocks.forEach((b, i) => {
        // 中途漏收了 delta 的段，在这里补上位置（排在末尾，总比不显示强）
        if (i >= base.blockOffset && b) timeline = appendOnce(timeline, { kind: "text", block: i });
      });
      return { ...base, content, text: blocks.join(""), blocks, timeline };
    }

    case EventType.TodosUpdated:
      // 规则 1：整表替换。待办卡片停在它第一次出现的位置
      return {
        ...base,
        todos: (d as unknown as TodosUpdatedData).todos ?? [],
        timeline: appendOnce(base.timeline, { kind: "todos" }),
      };

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
        timeline: appendOnce(base.timeline, { kind: "tool", callId: t.call_id }),
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
          deniedReason: t.denied_reason,
          fallbackApplied: t.fallback_applied,
        }),
      };
    }

    case EventType.AgentMode:
      return withMode(base, event.run_id, d as unknown as AgentModeData);

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
        timeline: appendOnce(base.timeline, { kind: "subagent", runId: s.subagent_run_id }),
      };
    }

    case EventType.SubagentFinished: {
      const runId = String(d.subagent_run_id ?? "");
      const failed = Boolean(d.failed) || d.status === "error";
      // ★ 挂起不是完成：子智能体还在跑，这一轮只是不再占着进程等它。
      //   标成 ok 的话卡片会显示「已完成」，而结论其实还没回来 ——
      //   用户看到的是一个自相矛盾的界面。
      const stillWaiting = Boolean(d.suspended);
      return {
        ...base,
        subagents: upsertSubagent(base.subagents, runId, {
          status: stillWaiting ? "running" : failed ? "error" : "ok",
          result: typeof d.result === "string" && d.result ? d.result : undefined,
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

    case EventType.SkillLoaded:
    case EventType.SkillSkipped:
    case EventType.McpToolDrift: {
      const notice = { seq: event.thread_seq, type: event.type, data: d } as unknown as Notice;
      return {
        ...base,
        notices: [...base.notices, notice],
        timeline: appendOnce(base.timeline, { kind: "notice", seq: event.thread_seq }),
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
        timeline: appendOnce(base.timeline, { kind: "compaction", seq: event.seq }),
      };
    }

    case EventType.ApprovalRequired: {
      const a = d as unknown as ApprovalRequiredData;
      return withApproval(base, {
        approvalId: a.approval_id,
        toolName: a.tool_name,
        args: a.args,
        reason: a.reason ?? "",
        // ★ native 的事件 data 里没有 run_id —— 回落到事件自身的 run_id，
        //   否则终态出列（withoutApprovalsOf）按 runId 匹配不上它。
        runId: a.run_id ?? event.run_id,
      });
    }

    case EventType.TitleGenerated:
      return { ...base, generatedTitle: String(d.title ?? "") };

    case EventType.RunFinished:
      return {
        ...base,
        status: "succeeded",
        pendingApprovals: withoutApprovalsOf(base.pendingApprovals, event.run_id),
        subagents: settleSubagents(base.subagents, "ok"),
      };

    case EventType.RunFailed: {
      const f = d as unknown as RunFailedData;
      return {
        ...base,
        status: "failed",
        pendingApprovals: withoutApprovalsOf(base.pendingApprovals, event.run_id),
        subagents: settleSubagents(base.subagents, "error"),
        error: {
          kind: f.error_kind ?? "unknown",
          message: f.message ?? "运行失败",
          partialText: f.partial_text,
        },
      };
    }

    case EventType.RunCancelled:
      return {
        ...base,
        status: "cancelled",
        pendingApprovals: withoutApprovalsOf(base.pendingApprovals, event.run_id),
        subagents: settleSubagents(base.subagents, "error"),
      };

    case EventType.RunSuspended:
      // 等子智能体。流不关，后面还会来这一轮真正的结束。
      return { ...base, status: "suspended" };

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
