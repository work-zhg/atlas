"use client";

import { Alert, Button, Skeleton, Tag } from "antd";
import { useEffect, useRef, type ReactNode } from "react";

import { useRunTrace } from "../api/runs";
import type { Message } from "../api/types";
import { Icon } from "../components/Icon";
import { Markdown } from "../components/Markdown";
import { relativeTime } from "../lib/format";
import { APPROVAL_MODE_LABEL, type AgentModeData, type ApprovalMode } from "../lib/events";
import {
  isStreamDone,
  orphanSubRuns,
  type Notice,
  type RunState,
  type SubagentRun,
  type TimelineItem,
  type ToolCall,
} from "../lib/run-reducer";
import { avatarBackground, avatarLetter } from "../lib/avatar";
import { ApprovalCard } from "./ApprovalCard";
import { CompactionDivider } from "./CompactionDivider";
import styles from "./chat.module.css";
import { QuestionCard } from "./QuestionCard";
import { SubAgentCard } from "./SubAgentCard";
import { MessageActions } from "./MessageActions";
import { Steps } from "./Steps";
import { TodoCard } from "./TodoCard";

interface Props {
  messages: Message[];
  loading: boolean;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  /** 当前 run 的实时状态；无进行中的 run 时为 undefined */
  live?: RunState;
  /** 实时块对应的 run —— 用于判断持久化消息是否已覆盖它，避免重复渲染 */
  liveRunId?: string;
  agentName: string;
  agentAvatarKey: string;
  reconnecting: boolean;
  streamError: Error | null;
  /** 会话 id：回答下的反馈要用 */
  threadId: string;
  /** 回答下的反馈按钮（嵌入方没有反馈接口时关掉） */
  feedback?: boolean;
  /** 重新生成：用最后一个问题再问一次。不给 = 不显示这个按钮 */
  onRegenerate?: () => void;
}

function textOf(content: Message["content"]): string {
  return blocksOf(content).join("");
}

/**
 * 正文分段。一个 run 里模型可能说好几轮 —— 调工具前一段、拿到结果后一段，
 * 后端按轮切成多个 text block（server 的 domain/events.py::Answer）。
 * 这里不再 join：最后一段是结论，前面的是过程，要分开渲染。
 */
function blocksOf(content: Message["content"]): string[] {
  return content
    .filter((b) => b.type === "text" && typeof b.text === "string")
    .map((b) => b.text as string)
    .filter((t) => t.length > 0);
}

/** 助手正文：末段是结论，之前的都是过程性发言，弱化显示。 */
function AssistantText({ blocks }: { blocks: string[] }) {
  if (blocks.length === 0) return null;
  // ★ ?? ""：noUncheckedIndexedAccess 下下标访问是 string | undefined
  const answer = blocks[blocks.length - 1] ?? "";
  const aside = blocks.slice(0, -1);
  return (
    <>
      {aside.map((t, i) => (
        // eslint-disable-next-line react/no-array-index-key -- 段序即身份，段不会重排
        <Markdown key={i} className={`${styles.prose} ${styles.proseAside}`}>{t}</Markdown>
      ))}
      <Markdown className={styles.prose}>{answer}</Markdown>
    </>
  );
}

/**
 * 实时区的一项。按出现顺序排（reducer 的 timeline），不按类型分组。
 *
 * ★ 正文段落要区分「过程」与「结论」：后面还跟着工具、委派、审批的那段话是
 *   过程（弱化显示）；排在最后、后面什么都没有的那段才是结论。
 * ★ live 是这一项所属 run 的状态（主轮次或某个子 run），root 是主轮次的：
 *   子 run 的过程（subRuns）与「审批还在等」（pendingApprovals）只在 root 上有。
 */
function LiveItem({
  item,
  live,
  root,
  isLast,
  streaming,
}: {
  item: TimelineItem;
  live: RunState;
  root: RunState;
  isLast: boolean;
  /** 这一轮还在进行（实时区）；历史轮次为 false */
  streaming: boolean;
}) {
  switch (item.kind) {
    case "text": {
      const text = live.blocks[item.block] ?? "";
      if (!text) return null;
      return (
        <Markdown className={isLast ? styles.prose : `${styles.prose} ${styles.proseAside}`}>
          {text}
        </Markdown>
      );
    }
    case "tool":
      // 工具调用由 TurnTimeline 把连续的几个归成一组「步骤」，不走这里
      return null;
    case "subagent": {
      const run = live.subagents.find((s) => s.runId === item.runId);
      if (!run) return null;
      const subId = live.subRunOf[run.runId];
      return <SubRun run={run} sub={subId ? root.subRuns[subId] : undefined} root={root} streaming={streaming} />;
    }
    case "approval": {
      const approval = live.approvals.find((a) => a.approvalId === item.approvalId);
      if (!approval) return null;
      const waiting = root.pendingApprovals.some((a) => a.approvalId === item.approvalId);
      if (approval.kind === "question") {
        return (
          <QuestionCard
            approval={approval}
            waiting={waiting}
            answered={live.answers[approval.approvalId]}
          />
        );
      }
      return <ApprovalCard approval={approval} waiting={waiting} />;
    }
    case "todos":
      return live.todos.length > 0 ? <TodoCard todos={live.todos} /> : null;
    case "compaction": {
      const mark = live.compactions.find((c) => c.seq === item.seq);
      return mark ? <CompactionDivider mark={mark} /> : null;
    }
    case "mode": {
      const mode = live.modes[item.runId];
      return mode ? <ModeBadge mode={mode} /> : null;
    }
    case "notice": {
      const notice = live.notices.find((n) => n.seq === item.seq);
      return notice ? <NoticeRow notice={notice} /> : null;
    }
  }
}

const DRIFT_TEXT: Record<string, string> = {
  used: "定义与保存时不一致，已照常使用新定义",
  blocked: "定义与保存时不一致，本轮未装载",
  pending_review: "定义尚未复核，本轮未装载",
  rejected: "定义复核未通过，本轮未装载",
};

/**
 * 技能 / MCP 的一行式提示（技能 / MCP 设计 §12）。
 *
 * ★ 被跳过、被拦下的必须显眼：静默少一个技能 / 工具的表现是模型「照常」做事，
 *   只是不按技能做、或换了条路 —— 用户不知道为什么结果变了。
 */
function NoticeRow({ notice }: { notice: Notice }) {
  if (notice.type === "skill.loaded") {
    return (
      <div className={styles.modeBadge}>
        <Icon name="spark" size={13} />
        加载技能 <Tag style={{ marginInlineEnd: 0 }}>{notice.data.slug}</Tag>
        <span className={styles.modeHint}>v{notice.data.version}</span>
      </div>
    );
  }
  if (notice.type === "skill.skipped") {
    return (
      <div className={styles.modeBadge}>
        <Icon name="warn" size={13} />
        技能 <Tag color="error" style={{ marginInlineEnd: 0 }}>{notice.data.slug} v{notice.data.version}</Tag>
        <span className={styles.modeHint}>
          {notice.data.reason === "revoked" ? "已被紧急下架，本轮未装载" : `未装载（${notice.data.reason}）`}
        </span>
      </div>
    );
  }
  if (notice.type === "knowledge.skipped") {
    return (
      <div className={styles.modeBadge}>
        <Icon name="book" size={13} />
        {/* 没有库 id = 整个知识库这一轮没用上（服务不可用），不是跳过了某几个库 */}
        {notice.data.dataset_ids.length > 0 && (
          <>
            知识库 <Tag color="warning" style={{ marginInlineEnd: 0 }}>跳过 {notice.data.dataset_ids.length} 个</Tag>
          </>
        )}
        <span className={styles.modeHint}>{notice.data.message}</span>
      </div>
    );
  }
  if (notice.type === "mcp.tool_skipped") {
    return (
      <div className={styles.modeBadge}>
        <Icon name="plug" size={13} />
        MCP <Tag color="error" style={{ marginInlineEnd: 0 }}>{notice.data.server}</Tag>
        <span className={styles.modeHint}>{notice.data.message}</span>
      </div>
    );
  }
  const used = notice.data.action === "used";
  return (
    <div className={styles.modeBadge}>
      <Icon name="plug" size={13} />
      MCP <Tag color={used ? "warning" : "error"} style={{ marginInlineEnd: 0 }}>{notice.data.tool}</Tag>
      <span className={styles.modeHint}>{DRIFT_TEXT[notice.data.action] ?? notice.data.action}</span>
    </div>
  );
}

const MODE_LABEL: Record<string, string> = {
  manual: "手动确认",
  accept_edits: "自动接受编辑",
  auto: "Auto",
  plan: "仅规划",
};
const modeLabel = (m: string | null) => (m ? (MODE_LABEL[m] ?? m) : "不支持");

/**
 * CLI 这一轮实际生效的权限模式。
 *
 * ★ 降级必须显眼：要的是 Auto、实际却是「自动接受编辑」时，命令仍会逐条请示 ——
 *   不说清楚，用户会以为 Auto 坏了。
 */
function ModeBadge({ mode }: { mode: AgentModeData }) {
  // native：计划获批，换成用户选的执行模式（doc/native-approval-mode-design.html §7）
  if (mode.source === "plan_approved") {
    return (
      <div className={styles.modeBadge}>
        计划已批准，以
        <Tag color="processing" style={{ marginInlineEnd: 0 }}>
          {APPROVAL_MODE_LABEL[mode.effective as ApprovalMode] ?? mode.effective}
        </Tag>
        模式执行
      </div>
    );
  }
  if (!mode.degraded) {
    return (
      <div className={styles.modeBadge}>
        CLI 权限模式：<Tag style={{ marginInlineEnd: 0 }}>{modeLabel(mode.effective)}</Tag>
      </div>
    );
  }
  return (
    <div className={styles.modeBadge}>
      CLI 权限模式：
      <Tag color="warning" style={{ marginInlineEnd: 0 }}>
        {modeLabel(mode.requested)} → {modeLabel(mode.effective)}
      </Tag>
      <span className={styles.modeHint}>
        {mode.effective
          ? "要求的模式未能生效，以实际模式运行"
          : "CLI 不支持权限模式，按它的默认行为运行"}
      </span>
    </div>
  );
}

/** 委派卡片，里面是子 run 自己的时间线（它再委派的卡片也在里面，层层嵌套）。 */
function SubRun({
  run,
  sub,
  root,
  streaming,
}: {
  run: SubagentRun;
  sub?: RunState;
  root: RunState;
  streaming: boolean;
}) {
  return (
    <SubAgentCard run={run} sub={sub}>
      {sub && <TurnTimeline state={sub} root={root} streaming={streaming} />}
    </SubAgentCard>
  );
}

const ORPHAN_STATUS: Record<RunState["status"], SubagentRun["status"]> = {
  idle: "running",
  running: "running",
  suspended: "running",
  succeeded: "ok",
  failed: "error",
  cancelled: "error",
};

/**
 * 一个 run 的过程时间线。实时区与历史轮次共用，子 run 的卡片里也是它。
 *
 * root 缺省 = state，即主轮次。找不到所属卡片的子 run（见 orphanSubRuns）排在主轮次末尾。
 */
function TurnTimeline({
  state,
  root = state,
  streaming,
}: {
  state: RunState;
  root?: RunState;
  streaming: boolean;
}) {
  const last = state.timeline.length - 1;
  const orphans = state === root ? orphanSubRuns(root) : [];

  // ★ 连续的工具调用归成一组「步骤」：中间夹了正文、委派、审批就另起一组 —— 先后顺序不能乱
  const out: ReactNode[] = [];
  let group: ToolCall[] = [];
  const flush = () => {
    if (group.length) out.push(<Steps key={`steps:${group[0]!.callId}`} calls={group} live={streaming} />);
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
    out.push(
      <LiveItem key={itemKey(item)} item={item} live={state} root={root} isLast={i === last} streaming={streaming} />,
    );
  });
  flush();

  return (
    <>
      {out}
      {orphans.map((id) => {
        const sub = root.subRuns[id]!;
        const run: SubagentRun = {
          runId: id,
          name: sub.meta?.agent_name || "子智能体",
          task: "",
          status: ORPHAN_STATUS[sub.status],
        };
        return <SubRun key={`orphan:${id}`} run={run} sub={sub} root={root} streaming={streaming} />;
      })}
    </>
  );
}

/**
 * 历史轮次的助手消息：有归档轨迹就按时间线重现过程（工具、委派、审批、待办），
 * 没有就退回只显示正文。
 *
 * ★ 轨迹来自 run_event 归档（GET /runs/{id}/trace）。归档被清理后返回空，
 *   timeline 为空 —— 这就是「数据清理后不再展示过程」的落点，不需要额外判断。
 * ★ 刚结束的那一轮先用实时状态顶上（fallback），轨迹取回后再替换 ——
 *   否则一轮结束的瞬间过程会先消失、再闪回来。
 */
function AssistantTurnBody({
  message,
  blocks,
  traceEnabled,
  fallback,
}: {
  message: Message;
  /** 这一轮所有助手消息的正文段（挂起续跑的一轮会落下好几条，合成一块显示） */
  blocks: string[];
  traceEnabled: boolean;
  fallback?: RunState;
}) {
  const traceQ = useRunTrace(message.run_id ?? undefined, traceEnabled);
  // ★ 只认 enabled 时的数据：同一个 key 的缓存在 enabled=false 时照样会返回，拿来用就会把整段过程画两遍
  const state = (traceEnabled ? traceQ.data : undefined) ?? fallback;
  if (!state || state.timeline.length === 0) return <AssistantText blocks={blocks} />;
  // ★ 历史轮次里不会再有待决定的审批：卡片一律显示「已结束」，也不去查库核对
  const settled: RunState = { ...state, pendingApprovals: [] };
  // acp 等拿不到 message.completed 的轮次，时间线里没有正文段 —— 用落库的正文补上
  const hasText = state.timeline.some((t) => t.kind === "text");
  return (
    <>
      <TurnTimeline state={settled} streaming={false} />
      {!hasText && <AssistantText blocks={blocks} />}
      {state.status === "failed" && state.error && (
        <Alert
          type="error"
          showIcon
          style={{ marginTop: "var(--s-3)" }}
          message={`运行失败（${state.error.kind}）`}
          description={state.error.message}
        />
      )}
      {state.status === "cancelled" && (
        <Alert type="info" showIcon style={{ marginTop: "var(--s-3)" }} message="已取消" />
      )}
    </>
  );
}

function itemKey(item: TimelineItem): string {
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

export function MessageStream({
  messages,
  loading,
  hasMore,
  loadingMore,
  onLoadMore,
  live,
  liveRunId,
  agentName,
  agentAvatarKey,
  reconnecting,
  streamError,
  threadId,
  feedback = false,
  onRegenerate,
}: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const liveText = live?.text ?? "";

  // 新内容到达时贴底。仅在用户已经接近底部时才自动滚 ——
  // 否则会把正在往回翻历史的用户强行拽下来。
  const bodyRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    if (nearBottom) bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages.length, liveText, live?.timeline.length]);

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
  // ★ 分页边界可能把一轮切成两条消息：轨迹只挂在第一条上，避免整段过程出现两遍
  const tracedRuns = new Set<string>();
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

  return (
    <div className={styles.paneBody} ref={bodyRef}>
      {loading && (
        <div style={{ maxWidth: 820, margin: "0 auto", padding: "var(--s-6)" }}>
          <Skeleton active paragraph={{ rows: 4 }} />
        </div>
      )}

      {hasMore && (
        <div style={{ display: "flex", justifyContent: "center", padding: "var(--s-4)" }}>
          <Button size="small" loading={loadingMore} onClick={onLoadMore}>
            加载更早的消息
          </Button>
        </div>
      )}

      {shown.map((m) =>
        m.role === "assistant" && firstOfRun.get(runOf(m)) !== m.id ? null : m.role === "user" ? (
          <div key={m.id} className={`${styles.msg} ${styles.msgUser}`}>
            <div className={styles.bubble}>{textOf(m.content)}</div>
          </div>
        ) : (
          <div key={m.id} className={styles.msg}>
            <div className={styles.msgHead}>
              <span
                className={styles.agentAvatar}
                style={{ background: avatarBackground(agentAvatarKey) }}
                aria-hidden="true"
              >
                {avatarLetter(agentName)}
              </span>
              <span className={styles.msgName}>{agentName}</span>
              <span className={styles.msgMeta}>{relativeTime(m.created_at)}</span>
            </div>
            <AssistantTurnBody
              message={m}
              blocks={runBlocks.get(runOf(m)) ?? []}
              traceEnabled={(() => {
                // 进行中（含挂起）的那一轮走实时区，不取轨迹 —— 否则会缓存一份残缺的
                if (!m.run_id || (m.run_id === liveRunId && !turnOver)) return false;
                if (tracedRuns.has(m.run_id)) return false;
                tracedRuns.add(m.run_id);
                return true;
              })()}
              fallback={m.run_id && m.run_id === liveRunId && turnOver ? live : undefined}
            />
            <MessageActions
              threadId={threadId}
              messageId={lastOfRun.get(runOf(m)) ?? m.id}
              text={(runBlocks.get(runOf(m)) ?? []).join("\n\n")}
              feedback={feedback}
              onRegenerate={lastAnswer && runOf(lastAnswer) === runOf(m) && !showLive ? onRegenerate : undefined}
            />
          </div>
        ),
      )}

      {showLive && live && (
        <div className={styles.msg}>
          <div className={styles.msgHead}>
            <span
              className={styles.agentAvatar}
              style={{ background: avatarBackground(agentAvatarKey) }}
              aria-hidden="true"
            >
              {avatarLetter(agentName)}
            </span>
            <span className={styles.msgName}>{agentName}</span>
            {reconnecting && (
              <Tag color="warning" style={{ marginInlineEnd: 0 }}>
                重新连接中…
              </Tag>
            )}
          </div>

          {/* §13.2：请求了但未接入的工具必须报出来，不能静默忽略 */}
          {live.meta?.unsupported_tools && live.meta.unsupported_tools.length > 0 && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: "var(--s-4)" }}
              message={`以下工具本期未接入，本轮不会被调用：${live.meta.unsupported_tools.join("、")}`}
            />
          )}

          {/* ★ 按发生的先后排：先说的话、再发起的委派、中途的审批，各在各的位置。
              压缩（§7.5）、待办也在其中 —— 它们同样有「在哪一步发生」的含义。 */}
          <TurnTimeline state={live} streaming={!isStreamDone(live.status)} />

          {live.status === "running" && <span className={styles.cursor} aria-hidden="true" />}

          {/*
            等子智能体。★ 必须说出来：一次委派可以跑一小时，这段时间里事件流
            一个字都不产出 —— 不提示的话用户分不清「在等」和「挂了」。
          */}
          {live.status === "suspended" && live.pendingApprovals.length === 0 && (
            <Alert
              type="info"
              showIcon
              style={{ marginTop: "var(--s-3)" }}
              message="正在等待子智能体完成，这一轮还没结束"
            />
          )}

          {live.status === "failed" && live.error && (
            <Alert
              type="error"
              showIcon
              style={{ marginTop: "var(--s-3)" }}
              message={`运行失败（${live.error.kind}）`}
              description={live.error.message}
            />
          )}

          {live.status === "cancelled" && (
            <Alert type="info" showIcon style={{ marginTop: "var(--s-3)" }} message="已取消" />
          )}
        </div>
      )}

      {streamError && (
        <div className={styles.msg}>
          <Alert
            type="error"
            showIcon
            icon={<Icon name="warn" size={16} />}
            message="事件流连接失败"
            description={streamError.message}
          />
        </div>
      )}

      <div ref={bottomRef} style={{ height: 1 }} />
    </div>
  );
}
