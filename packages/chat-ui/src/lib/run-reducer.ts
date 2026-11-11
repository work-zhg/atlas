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
  type ContextClearedData,
  type ContextCompactedData,
  type ContextUsageData,
  type FileWrittenData,
  type McpToolDriftData,
  type McpToolSkippedData,
  type KnowledgeSkippedData,
  type SkillLoadedData,
  type SkillSkippedData,
  type MessageCompletedData,
  type MessageDeltaData,
  type RunFailedData,
  type RunResumedData,
  type ApprovalMode,
  type ApprovalRequiredData,
  type QuestionAnsweredData,
  type RunStartedData,
  type RunSuspendedData,
  type SubagentStartedData,
  type Todo,
  type ToolArtifact,
  type TodosUpdatedData,
  type ToolCompletedData,
  type ToolFailedData,
  type ToolStartedData,
  type ToolUpdatedData,
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
  /** 工具给界面的结构化结果（tool.completed 的 artifact） */
  artifact?: ToolArtifact;
  error?: string;
  errorKind?: string;
  /** errorKind = "auto_mode_denied" 时：auto 模式的拦截类别 */
  deniedReason?: string;
  fallbackApplied?: boolean;
  /**
   * = run 在委派树里的深度：0 主 agent，1 子智能体。
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
  /** 模型给的短标题；task 才是 SubAgent 收到的任务书 */
  title?: string;
  task: string;
  status: "running" | "ok" | "error";
  result?: string;
  durationMs?: number;
}

export interface PendingApproval {
  approvalId: string;
  kind: "tool" | "plan" | "question";
  /** 对应的工具调用（问题卡片据此收起那一行工具调用） */
  toolCallId?: string;
  toolName: string;
  args: unknown;
  reason: string;
  /** 计划获批时可选的执行模式；kind = tool 时为空 */
  executionModes: ApprovalMode[];
  /** 拒绝时可以带原因（交给模型）。acp 的审批为 false */
  acceptsFeedback: boolean;
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
  | { seq: number; type: "mcp.tool_drift"; data: McpToolDriftData }
  | { seq: number; type: "mcp.tool_skipped"; data: McpToolSkippedData }
  | { seq: number; type: "knowledge.skipped"; data: KnowledgeSkippedData };

export interface CompactionMark {
  seq: number;
  /** compacted = 压缩成摘要；cleared = 清除了旧的工具结果 */
  variant?: "compacted" | "cleared";
  messagesSummarized: number;
  tokensBefore: number;
  tokensAfter: number;
  summary: string;
  degraded: boolean;
  reason?: string;
  restoredFiles?: string[];
  cleared?: number;
  tokensFreed?: number;
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
   * 这个 run 委派出去的子智能体（卡片）。只有边界：subagent.started 给任务书，
   * subagent.finished 给结论；子智能体的过程在 subRuns 里。
   */
  subagents: SubagentRun[];
  usage: Usage;
  /**
   * 子智能体的过程：每个子 run 一份与主轮次同形的状态，按**子 run 的 run_id**。
   *
   * ★ 刻意与主对话分开：混进去的话用户会看到一段语气都不一样的话突然冒出来。
   * ★ 只在主轮次的状态上有值，孙 run 也平铺在这里；子 run 状态里的这一项恒为空。
   */
  subRuns: Record<string, RunState>;
  /**
   * task 调用（`SubagentRun.runId`）→ 子 run 的 run_id，卡片据此找到 subRuns 里的过程。
   * 来自这个 run 的 run.suspended（waiting_on 的 token 就是子 run id）。
   */
  subRunOf: Record<string, string>;
  /** 当前这一轮的 run —— 取消按钮、GET /runs/{id} 都用它。 */
  activeRunId?: string;
  /** 已处理的最大 thread_seq —— **会话级**游标，断线续传用 */
  lastSeq: number;
  /**
   * 跳过了、还没收到的 thread_seq（最多 MAX_MISSING 个）。晚到的事件靠它进来，见 applyEvent。
   */
  missingSeqs: number[];
  meta?: RunStartedData;
  error?: { kind: string; message: string; partialText?: string };
  compactions: CompactionMark[];
  /** 最近一次模型调用的上下文用量（主智能体的；子智能体的不覆盖它） */
  contextUsage?: ContextUsageData;
  /** 技能 / MCP 的一行式提示（按 thread_seq 标识，跨 run 唯一） */
  notices: Notice[];
  /** 还在等人决定的审批（§12.2）。run 走到终态时出列。 */
  pendingApprovals: PendingApproval[];
  /**
   * 这一轮出现过的**全部**审批，不出列 —— 对话里的审批卡片要一直留在原位，
   * 处理完显示结果，而不是消失。
   */
  approvals: PendingApproval[];
  /** 已回答的问题，按 approvalId（question.answered；进轨迹，刷新后也在） */
  answers: Record<string, QuestionAnsweredData>;
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
  subRuns: {},
  subRunOf: {},
  lastSeq: 0,
  missingSeqs: [],
  compactions: [],
  notices: [],
  pendingApprovals: [],
  approvals: [],
  answers: {},
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
/** 事件 → 卡片要的字段（runId 由调用方按流的上下文补） */
function approvalOf(a: ApprovalRequiredData): Omit<PendingApproval, "runId"> {
  return {
    approvalId: a.approval_id,
    kind: a.approval_kind ?? "tool",
    toolCallId: a.tool_call_id,
    toolName: a.tool_name,
    args: a.args,
    reason: a.reason ?? "",
    executionModes: a.execution_modes ?? [],
    acceptsFeedback: a.accepts_feedback ?? false,
  };
}

/** 问题回答了：记下答案，并从「还在等」里拿掉 —— run 继续跑，头部不该还显示「待回答」 */
function withAnswer(base: RunState, answered: QuestionAnsweredData): RunState {
  return {
    ...base,
    answers: { ...base.answers, [answered.approval_id]: answered },
    pendingApprovals: base.pendingApprovals.filter((a) => a.approvalId !== answered.approval_id),
  };
}

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
 * 子智能体的事件（depth≥1）：归约进**它自己那个 run** 的状态（subRuns），不进主轮次。
 *
 * ★ 与主轮次用同一个归约（applyRunEvent）：子智能体说的话、工具、待办、技能提示、
 *   压缩、审批、它再委派出去的 SubAgent，在它的卡片里与主对话同样呈现。
 *
 * ★ 不隔离进主轮次的后果是两个具体的 bug：子 run 的 run.finished 会把主轮次标成
 *   已结束（取消按钮消失、消息列表提前刷新），它的 message.delta 会让子智能体的话
 *   直接冒进主对话气泡。
 *
 * 主轮次只额外记一样：**还在等人决定的审批**。头部的「待审批」要算上子智能体的，
 * 卡片的「还在等」也以主轮次这张表为准（历史轮次整张清空即可）。子智能体请求权限时
 * 事件带着子 run 的 run_id，ApprovalCard 据此提交到子 run 的端点
 * （doc/detail/suspension.html §06）。
 */
function applySubEvent(base: RunState, event: AnyTraceEvent): RunState {
  const sub = applyRunEvent(base.subRuns[event.run_id] ?? initialRunState, event);
  const next = { ...base, subRuns: { ...base.subRuns, [event.run_id]: sub } };

  switch (event.type) {
    case EventType.ApprovalRequired: {
      const id = (event.data as ApprovalRequiredData).approval_id;
      const approval = sub.approvals.find((a) => a.approvalId === id);
      if (!approval || base.pendingApprovals.some((a) => a.approvalId === id)) return next;
      return { ...next, pendingApprovals: [...base.pendingApprovals, approval] };
    }

    case EventType.QuestionAnswered: {
      const id = (event.data as QuestionAnsweredData).approval_id;
      return { ...next, pendingApprovals: base.pendingApprovals.filter((a) => a.approvalId !== id) };
    }

    case EventType.RunFinished:
    case EventType.RunFailed:
    case EventType.RunCancelled:
      // 子 run 结束**不**改主轮次的状态，只把它名下的审批出列
      return { ...next, pendingApprovals: withoutApprovalsOf(base.pendingApprovals, event.run_id) };

    default:
      return next;
  }
}

/**
 * 找不到所属卡片的子 run。首连只回放最近一段事件，派出它的 subagent.started /
 * run.suspended 可能不在里面 —— 不单独显示的话，刷新后子智能体的过程整个看不见。
 *
 * ★ 还有卡片在等对应关系（task 调了、run.suspended 还没到）时一个也不报：子 run 一派出
 *   就开始出事件，比父 run 的 run.suspended 早到一会儿，这时报出来卡片会先在末尾闪一下。
 */
export function orphanSubRuns(root: RunState): string[] {
  const states = [root, ...Object.values(root.subRuns)];
  const awaiting = states.some((s) =>
    s.subagents.some((c) => c.status === "running" && !s.subRunOf[c.runId]),
  );
  if (awaiting) return [];
  const mapped = new Set(states.flatMap((s) => Object.values(s.subRunOf)));
  return Object.keys(root.subRuns).filter((id) => !mapped.has(id));
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
function freshTurn(): Omit<RunState, "lastSeq" | "missingSeqs" | "activeRunId"> {
  const { lastSeq: _l, missingSeqs: _m, activeRunId: _a, ...rest } = initialRunState;
  return rest;
}

/** 记多少个「跳过了、还没到」的序号。并发发布的交错只有几条，留足余量 */
const MAX_MISSING = 64;

export function applyEvent(state: RunState, event: AnyTraceEvent): RunState {
  // 规则 2：thread_seq 在会话内唯一。重连边界可能重发，收过的直接丢弃。
  //
  // ★ 游标是 thread_seq 而不是 seq：会话流上并发的两个 run（父挂起 + 子在跑）
  //   各有自己的 seq 序列，拿 seq 去重会把后来的整段丢掉。
  // ★ 到达顺序不一定是 thread_seq 的顺序：终态 / 挂起事件先领序号、落库后才发布
  //   （relay.emit），这期间刚派出的子 run 已经在发事件了 —— 父 run 的 run.suspended
  //   比子 run 的头几条序号小、却晚到。按「≤ 最大值就丢」去重会把它丢掉，卡片从此
  //   找不到子 run，过程要刷新（按序号重放）才出来。所以跳过的序号先记下，晚到时照收。
  const seq = event.thread_seq;
  let missingSeqs = state.missingSeqs;
  if (seq > state.lastSeq) {
    // 首连（lastSeq 为 0）从回放窗口中间开始，前面的不算缺
    if (state.lastSeq > 0 && seq > state.lastSeq + 1) {
      const from = Math.max(state.lastSeq + 1, seq - MAX_MISSING);
      const skipped = Array.from({ length: seq - from }, (_, i) => from + i);
      missingSeqs = [...missingSeqs, ...skipped].slice(-MAX_MISSING);
    }
  } else if (missingSeqs.includes(seq)) {
    missingSeqs = missingSeqs.filter((s) => s !== seq);
  } else {
    return state;
  }

  const base = { ...state, lastSeq: Math.max(state.lastSeq, seq), missingSeqs };
  // 子智能体的事件（depth≥1）进它自己的状态，不进主轮次（见 applySubEvent）
  return event.depth > 0 ? applySubEvent(base, event) : applyRunEvent(base, event);
}

/** 一个 run 自己的事件 → 它的状态。主轮次与每个子 run 共用（子 run 的状态在 subRuns 里）。 */
function applyRunEvent(base: RunState, event: AnyTraceEvent): RunState {
  const d = event.data as Record<string, unknown>;

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
        missingSeqs: base.missingSeqs,
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

    case EventType.ToolUpdated: {
      // 参数 / 名字补全：合并进已有的行。缺席的字段由 upsertTool 的 definedOnly 跳过，不会抹掉旧值
      const t = d as unknown as ToolUpdatedData;
      return {
        ...base,
        toolCalls: upsertTool(base.toolCalls, t.call_id, {
          name: t.name,
          argsPreview: t.args_preview,
          args: t.args,
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
          artifact: t.artifact,
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
          title: s.title || undefined,
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
    case EventType.McpToolDrift:
    case EventType.McpToolSkipped:
    case EventType.KnowledgeSkipped: {
      const notice = { seq: event.thread_seq, type: event.type, data: d } as unknown as Notice;
      return {
        ...base,
        notices: [...base.notices, notice],
        timeline: appendOnce(base.timeline, { kind: "notice", seq: event.thread_seq }),
      };
    }

    case EventType.ContextCompacted: {
      const c = d as unknown as ContextCompactedData;
      // ★ 用会话级 thread_seq 当键：run 内的 seq 跨 run 会重复，两次压缩会被当成同一条
      const seq = event.thread_seq || event.seq;
      return {
        ...base,
        compactions: [
          ...base.compactions,
          {
            seq,
            variant: "compacted",
            messagesSummarized: c.messages_summarized,
            tokensBefore: c.tokens_before,
            tokensAfter: c.tokens_after,
            summary: c.summary,
            degraded: Boolean(c.degraded),
            reason: c.reason,
            restoredFiles: c.restored_files ?? [],
          },
        ],
        timeline: appendOnce(base.timeline, { kind: "compaction", seq }),
      };
    }

    case EventType.ContextCleared: {
      const c = d as unknown as ContextClearedData;
      const seq = event.thread_seq || event.seq;
      return {
        ...base,
        compactions: [
          ...base.compactions,
          {
            seq,
            variant: "cleared",
            messagesSummarized: 0,
            tokensBefore: 0,
            tokensAfter: 0,
            summary: "",
            degraded: false,
            reason: c.reason,
            cleared: c.cleared,
            tokensFreed: c.tokens_freed,
          },
        ],
        timeline: appendOnce(base.timeline, { kind: "compaction", seq }),
      };
    }

    case EventType.ContextUsage: {
      // 子智能体的用量不覆盖主会话的（它在自己的子会话里）
      if (event.depth > 0) return base;
      return { ...base, contextUsage: d as unknown as ContextUsageData };
    }

    case EventType.ApprovalRequired: {
      const a = d as unknown as ApprovalRequiredData;
      return withApproval(base, {
        ...approvalOf(a),
        // ★ native 的事件 data 里没有 run_id —— 回落到事件自身的 run_id，
        //   否则终态出列（withoutApprovalsOf）按 runId 匹配不上它。
        runId: a.run_id ?? event.run_id,
      });
    }

    case EventType.QuestionAnswered:
      return withAnswer(base, d as unknown as QuestionAnsweredData);

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

    case EventType.RunSuspended: {
      // 等子智能体。流不关，后面还会来这一轮真正的结束。
      // ★ waiting_on 里有 task 调用 → 子 run 的对应，卡片靠它找到子智能体的过程。
      //   合并而不是替换：一轮可以先后挂起几次、每次等不同的子智能体
      const waits = (d as unknown as RunSuspendedData).waiting_on ?? [];
      const subRunOf = { ...base.subRunOf };
      for (const w of waits) {
        if (w.reason === "subagent" && w.tool_call_id) subRunOf[w.tool_call_id] = w.token;
      }
      return { ...base, status: "suspended", subRunOf };
    }

    case EventType.RunResumed: {
      // 换了一个服务实例继续跑。上一个实例最后一次模型调用已经流出、没落库的那段正文
      // 会被重做 —— 从末尾丢掉它，否则同一段话出现两遍。后面紧跟新一段的 run.started。
      if (event.run_id !== base.activeRunId) return base;
      let discard = (d as unknown as RunResumedData).discard_chars ?? 0;
      if (discard <= 0) return base;
      const blocks = [...base.blocks];
      for (let i = blocks.length - 1; i >= base.blockOffset && discard > 0; i--) {
        const keep = Math.max(blocks[i]!.length - discard, 0);
        discard -= blocks[i]!.length - keep;
        blocks[i] = blocks[i]!.slice(0, keep);
      }
      return { ...base, blocks, text: blocks.join("") };
    }

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
