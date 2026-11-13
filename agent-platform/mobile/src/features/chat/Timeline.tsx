/**
 * 一个 run 的过程时间线、一轮回答的正文（同网页 chat-ui 的 MessageStream）。
 * 摆法在 chat-ui 的 lib/conversation.ts（两端共用）；这里只负责画。
 */
import { StyleSheet, Text, View } from "react-native";

import { useRunTrace } from "@atlas/chat-ui/src/api/runs";
import type { Message } from "@atlas/chat-ui/src/api/types";
import { timelineEntries } from "@atlas/chat-ui/src/lib/conversation";
import type { RunState, TimelineItem } from "@atlas/chat-ui/src/lib/run-reducer";
import { Markdown } from "@/components/Markdown";
import { Icon } from "@/components/ui";
import { C, F } from "@/theme";
import { ApprovalCard, CompactionDivider, ModeBadge, NoticeRow, QuestionCard, SubAgentCard, TodoCard } from "./cards";
import { Steps } from "./Steps";

/** 助手正文：末段是结论，之前的是过程性发言，弱化显示 */
export function AssistantText({ blocks }: { blocks: string[] }) {
  if (blocks.length === 0) return null;
  return (
    <>
      {blocks.slice(0, -1).map((t, i) => (
        <Markdown key={i} aside>
          {t}
        </Markdown>
      ))}
      <Markdown>{blocks[blocks.length - 1] ?? ""}</Markdown>
    </>
  );
}

function LiveItem({ item, live, root, isLast }: { item: TimelineItem; live: RunState; root: RunState; isLast: boolean }) {
  switch (item.kind) {
    case "text": {
      const text = live.blocks[item.block] ?? "";
      return text ? <Markdown aside={!isLast}>{text}</Markdown> : null;
    }
    case "tool":
      return null;
    case "subagent": {
      const run = live.subagents.find((x) => x.runId === item.runId);
      if (!run) return null;
      const subId = live.subRunOf[run.runId];
      return <SubAgentCard run={run} subId={subId} sub={subId ? root.subRuns[subId] : undefined} root={root} />;
    }
    case "approval": {
      const approval = live.approvals.find((a) => a.approvalId === item.approvalId);
      if (!approval) return null;
      const waiting = root.pendingApprovals.some((a) => a.approvalId === item.approvalId);
      return approval.kind === "question" ? (
        <QuestionCard approval={approval} waiting={waiting} answered={live.answers[approval.approvalId]} />
      ) : (
        <ApprovalCard approval={approval} waiting={waiting} />
      );
    }
    case "todos":
      return <TodoCard todos={live.todos} />;
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

/** 一个 run 的过程。root 缺省 = state（主轮次）；子 run 的过程页传子 run 的状态和主轮次 */
export function TurnTimeline({ state, root = state, streaming }: { state: RunState; root?: RunState; streaming: boolean }) {
  return (
    <>
      {timelineEntries(state, root).map((e) => {
        if (e.kind === "steps") return <Steps key={e.key} calls={e.calls} live={streaming} />;
        if (e.kind === "orphan") return <SubAgentCard key={e.key} run={e.run} subId={e.run.runId} sub={e.sub} root={root} />;
        return <LiveItem key={e.key} item={e.item} live={state} root={root} isLast={e.isLast} />;
      })}
    </>
  );
}

/** 结束状态的提示（失败、取消） */
export function TurnEnd({ state }: { state: RunState }) {
  if (state.status === "failed" && state.error) {
    return (
      <View style={s.err}>
        <Icon name="alert" size={18} color={C.dangerInk} />
        <Text style={s.errText}>{state.error.message || "这一轮没有完成"}</Text>
      </View>
    );
  }
  if (state.status === "cancelled") return <Text style={s.note}>已取消</Text>;
  return null;
}

/**
 * 历史轮次：有归档轨迹就按时间线重现过程，没有就只显示正文（同网页 AssistantTurnBody）。
 * ★ 刚结束的那一轮先用实时状态顶上（fallback），轨迹取回后再替换。
 */
export function AssistantTurnBody({
  message,
  blocks,
  traceEnabled,
  fallback,
}: {
  message: Message;
  blocks: string[];
  traceEnabled: boolean;
  fallback?: RunState;
}) {
  const traceQ = useRunTrace(message.run_id ?? undefined, traceEnabled);
  const state = (traceEnabled ? traceQ.data : undefined) ?? fallback;
  if (!state || state.timeline.length === 0) return <AssistantText blocks={blocks} />;
  // 历史轮次里不会再有待决定的审批：卡片一律显示「已结束」
  const settled: RunState = { ...state, pendingApprovals: [] };
  const hasText = state.timeline.some((t) => t.kind === "text");
  return (
    <>
      <TurnTimeline state={settled} streaming={false} />
      {!hasText && <AssistantText blocks={blocks} />}
      <TurnEnd state={state} />
    </>
  );
}

const s = StyleSheet.create({
  err: { flexDirection: "row", gap: 10, alignItems: "flex-start", backgroundColor: C.dangerWeak, borderRadius: 14, padding: 12, marginBottom: 12 },
  errText: { flex: 1, color: C.dangerInk, fontSize: F.md, lineHeight: 21 },
  note: { fontSize: F.sm, color: C.ink3, marginBottom: 12 },
});
