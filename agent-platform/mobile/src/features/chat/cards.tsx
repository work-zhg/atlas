/**
 * 对话里的卡片：审批、提问、子智能体、执行计划、上下文压缩、技能 / MCP 提示、CLI 权限模式。
 *
 * 行为与网页（packages/chat-ui/src/chat/*Card.tsx）一致：在对话流里、出现在发生的那一步；没有等待上限；
 * 处理完留在原位显示结果；「还在不在等」以库为准（GET /runs/{id}/approvals）。
 * 手机上的差别只在样子：按钮 44 高、对半分；拒绝原因默认收起；子智能体的过程进单独一页。
 */
import { useState, type ReactNode } from "react";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { useApprovalStillPending, useDecideApproval } from "@atlas/chat-ui/src/api/runs";
import {
  APPROVAL_MODE_LABEL,
  type AgentModeData,
  type ApprovalMode,
  type AskUserQuestionArgs,
  type QuestionAnswerData,
  type QuestionAnsweredData,
  type QuestionSpec,
  type Todo,
} from "@atlas/chat-ui/src/lib/events";
import { compactNumber, durationMs } from "@atlas/chat-ui/src/lib/format";
import { ApiError } from "@atlas/chat-ui/src/lib/http";
import type { CompactionMark, Notice, PendingApproval, RunState, SubagentRun } from "@atlas/chat-ui/src/lib/run-reducer";
import { toolLabel } from "@atlas/chat-ui/src/lib/steps";
import { Markdown } from "@/components/Markdown";
import { Avatar, Button, Icon, Sheet, Spin, Tag, type IconName, type Tone } from "@/components/ui";
import { C, F, MONO, R } from "@/theme";
import { useChat } from "./context";

type ExecutionMode = Exclude<ApprovalMode, "plan">;

// ── 审批（工具调用 / 计划）

export function ApprovalCard({ approval, waiting }: { approval: PendingApproval; waiting: boolean }) {
  const decide = useDecideApproval();
  const [decided, setDecided] = useState<{ decision: "approved" | "rejected"; mode?: ExecutionMode } | null>(null);
  const [feedback, setFeedback] = useState("");
  const [showFeedback, setShowFeedback] = useState(false);
  const isPlan = approval.kind === "plan";
  const modes = approval.executionModes as ExecutionMode[];
  // 计划获批后的执行方式：默认最宽松的那个（按严格程度排，最后一个）
  const [planMode, setPlanMode] = useState<ExecutionMode | undefined>(modes[modes.length - 1]);
  // ★ 以库为准：刷新后首连回放会把早已处理过的 approval.required 再送来一遍
  const stillPending = useApprovalStillPending(waiting && !decided ? approval.runId : undefined, approval.approvalId);
  // 查库失败时宁可让用户能点：藏掉一个真在等的审批，run 就一直停在那里
  const canDecide = waiting && !decided && (stillPending.data === true || stillPending.isError);
  const closedElsewhere = waiting && !decided && stillPending.isSuccess && !stillPending.data;

  const submit = (decision: "approved" | "rejected", mode?: ExecutionMode) => {
    if (!approval.runId) return;
    const reason = feedback.trim();
    decide.mutate(
      {
        runId: approval.runId,
        approvalId: approval.approvalId,
        decision,
        approvalMode: mode,
        feedback: decision === "rejected" && reason ? reason : undefined,
      },
      {
        // ★ 只有提交成功才算已决策：网络失败时按钮要留着，用户才能重试
        onSuccess: () => setDecided({ decision, mode }),
        onError: (err) => {
          // 409 = 已在别处处理过（电脑上、或这一轮已经结束）
          if (err instanceof ApiError && err.status === 409) void stillPending.refetch();
        },
      },
    );
  };

  let status: { tone: Tone; label: string };
  if (decided?.decision === "approved") {
    status = { tone: "success", label: decided.mode ? `已批准 · ${APPROVAL_MODE_LABEL[decided.mode]}执行` : "已批准" };
  } else if (decided) status = { tone: "danger", label: isPlan ? "已退回修改" : "已拒绝" };
  else if (canDecide) status = { tone: "warning", label: "等待你确认" };
  else if (closedElsewhere) status = { tone: "default", label: "已处理" };
  else if (!waiting) status = { tone: "default", label: "已结束" };
  else status = { tone: "default", label: "核对中…" };

  return (
    <View style={[s.card, canDecide && s.cardWarn]}>
      <View style={s.cardHead}>
        <Icon name={isPlan ? "list" : "shield"} size={18} />
        <Text style={s.cardTitle}>{isPlan ? "计划待批准" : "需要你确认"}</Text>
        {!isPlan && (
          <Text style={s.muted} numberOfLines={1}>
            {toolLabel(approval.toolName)}
          </Text>
        )}
        <View style={{ marginLeft: "auto" }}>
          <Tag tone={status.tone}>{status.label}</Tag>
        </View>
      </View>
      {approval.reason && !isPlan ? <Text style={s.text}>{approval.reason}</Text> : null}
      {isPlan ? <PlanView args={approval.args} /> : <ArgsView name={approval.toolName} args={approval.args} />}

      {canDecide && isPlan && modes.length > 0 && (
        <>
          <Text style={s.label}>批准后怎么执行</Text>
          <View style={s.segm}>
            {modes.map((m) => (
              <Pressable key={m} style={[s.segmItem, planMode === m && s.segmOn]} onPress={() => setPlanMode(m)}>
                <Text style={[s.segmText, planMode === m && { color: C.ink, fontWeight: "700" }]}>{APPROVAL_MODE_LABEL[m]}</Text>
              </Pressable>
            ))}
          </View>
        </>
      )}

      {canDecide && approval.acceptsFeedback && (
        showFeedback || isPlan ? (
          <>
            <Text style={s.label}>{isPlan ? "修改意见（可选）" : "拒绝原因（可选）"}</Text>
            <TextInput
              style={s.input}
              multiline
              maxLength={4000}
              value={feedback}
              onChangeText={setFeedback}
              placeholder={isPlan ? "退回时交给智能体，它会继续完善计划" : "会告诉智能体为什么不行"}
              placeholderTextColor={C.ink4}
            />
          </>
        ) : (
          <Pressable onPress={() => setShowFeedback(true)} hitSlop={8}>
            <Text style={s.link}>写拒绝原因</Text>
          </Pressable>
        )
      )}

      {canDecide && (
        <>
          <View style={s.btns}>
            <Button kind="danger" style={{ flex: 1 }} loading={decide.isPending} onPress={() => submit("rejected")}>
              {isPlan ? (feedback.trim() ? "提修改意见" : "不批准") : "拒绝"}
            </Button>
            <Button
              kind="primary"
              style={{ flex: 1 }}
              loading={decide.isPending}
              onPress={() => submit("approved", isPlan ? planMode : undefined)}
            >
              批准
            </Button>
          </View>
          <Text style={s.hint}>
            {isPlan ? "批准后按所选方式在这一轮继续执行" : "拒绝不会中断这轮对话，智能体会换一种方式继续"}
          </Text>
        </>
      )}
      {decide.isError && !decided ? <Text style={s.error}>提交失败：{decide.error.message}</Text> : null}
    </View>
  );
}

function rec(args: unknown): Record<string, unknown> {
  return args && typeof args === "object" ? (args as Record<string, unknown>) : {};
}
function str(a: Record<string, unknown>, key: string): string | undefined {
  const v = a[key];
  return typeof v === "string" ? v : undefined;
}

function PlanView({ args }: { args: unknown }) {
  return <Markdown>{str(rec(args), "plan") ?? ""}</Markdown>;
}

/** 按工具显示调用参数；认不出的（MCP、CLI 的工具）显示 JSON */
function ArgsView({ name, args }: { name: string; args: unknown }) {
  const a = rec(args);
  const path = str(a, "file_path");
  const [open, setOpen] = useState(false);
  switch (name) {
    case "execute": {
      const command = str(a, "command");
      if (command !== undefined) return <Code>{command}</Code>;
      break;
    }
    case "write_file": {
      const content = str(a, "content");
      if (path === undefined || content === undefined) break;
      return (
        <View>
          <Pressable onPress={() => setOpen((o) => !o)} style={s.row}>
            <Text style={s.mono} numberOfLines={1}>
              {path}
            </Text>
            <Text style={s.muted}>
              {" "}
              · {content.length.toLocaleString()} 字符 {open ? "收起" : "展开"}
            </Text>
          </Pressable>
          {open && <Code>{content}</Code>}
        </View>
      );
    }
    case "edit_file": {
      const before = str(a, "old_string");
      const after = str(a, "new_string");
      if (path === undefined || before === undefined || after === undefined) break;
      return (
        <View>
          <Text style={s.mono}>
            {path}
            {a.replace_all === true ? "（全部替换）" : ""}
          </Text>
          <Text style={s.label}>修改前</Text>
          <Code tint={C.dangerWeak}>{before}</Code>
          <Text style={s.label}>修改后</Text>
          <Code tint={C.successWeak}>{after}</Code>
        </View>
      );
    }
    case "delete":
      if (path !== undefined) return <Text style={s.text}>删除 <Text style={s.mono}>{path}</Text></Text>;
      break;
    case "task": {
      const brief = str(a, "prompt");
      if (brief === undefined) break;
      return (
        <View>
          <Text style={s.label}>委派给</Text>
          <Text style={s.text}>
            {str(a, "subagent_type")} {str(a, "description") ? `· ${str(a, "description")}` : ""}
          </Text>
          <Text style={s.label}>任务书</Text>
          <Code>{brief}</Code>
        </View>
      );
    }
  }
  return <Code>{JSON.stringify(args, null, 2)}</Code>;
}

function Code({ children, tint }: { children: string; tint?: string }) {
  return (
    <Text selectable style={[s.code, tint ? { backgroundColor: tint } : null]}>
      {children}
    </Text>
  );
}

// ── 向用户提问

interface Draft {
  selected: string[];
  other: string;
}

/** 推荐项预选：用户只改不同意的那几题，直接提交即可 */
function initialDraft(q: QuestionSpec): Draft {
  return { selected: q.options.filter((o) => o.recommended).map((o) => o.label), other: "" };
}

export function QuestionCard({
  approval,
  waiting,
  answered,
}: {
  approval: PendingApproval;
  waiting: boolean;
  answered?: QuestionAnsweredData;
}) {
  const questions = (approval.args as AskUserQuestionArgs | undefined)?.questions ?? [];
  const decide = useDecideApproval();
  const [drafts, setDrafts] = useState<Draft[]>(() => questions.map(initialDraft));
  const [submitted, setSubmitted] = useState<QuestionAnswerData[] | null>(null);
  const stillPending = useApprovalStillPending(
    waiting && !submitted && !answered ? approval.runId : undefined,
    approval.approvalId,
  );
  const canAnswer = waiting && !submitted && !answered && (stillPending.data === true || stillPending.isError);
  const closedElsewhere = waiting && !submitted && !answered && stillPending.isSuccess && !stillPending.data;
  const setDraft = (i: number, patch: Partial<Draft>) => setDrafts((all) => all.map((d, j) => (j === i ? { ...d, ...patch } : d)));

  const submit = (skip: boolean) => {
    if (!approval.runId) return;
    const answers = drafts.map((d) => (skip ? { selected: [], other: null } : { selected: d.selected, other: d.other.trim() || null }));
    decide.mutate(
      { runId: approval.runId, approvalId: approval.approvalId, decision: "approved", answers },
      {
        onSuccess: () =>
          setSubmitted(
            answers.map((a, i) => ({
              header: questions[i]?.header ?? "",
              question: questions[i]?.question ?? "",
              ...a,
              by: a.selected.length || a.other ? "user" : "skipped",
            })),
          ),
        onError: (err) => {
          if (err instanceof ApiError && err.status === 409) void stillPending.refetch();
        },
      },
    );
  };

  const shown = answered?.answers ?? submitted;
  let status: { tone: Tone; label: string };
  if (answered || submitted) status = { tone: "success", label: "已回答" };
  else if (canAnswer) status = { tone: "warning", label: "等你回答" };
  else if (closedElsewhere) status = { tone: "default", label: "已处理" };
  else if (!waiting) status = { tone: "default", label: "已结束" };
  else status = { tone: "default", label: "核对中…" };

  return (
    <View style={[s.card, canAnswer && s.cardWarn]}>
      <View style={s.cardHead}>
        <Icon name="help" size={18} />
        <Text style={s.cardTitle}>向你提问</Text>
        <View style={{ marginLeft: "auto" }}>
          <Tag tone={status.tone}>{status.label}</Tag>
        </View>
      </View>
      {shown ? (
        shown.length === 0 ? (
          <Text style={s.text}>{answered?.feedback ? `没有按选项回答：${answered.feedback}` : "没有回答，交给智能体决定"}</Text>
        ) : (
          shown.map((a, i) => (
            <View key={i}>
              <Text style={s.label}>{a.header || a.question}</Text>
              <Text style={s.text}>{answerText(a)}</Text>
            </View>
          ))
        )
      ) : canAnswer ? (
        <>
          {questions.map((q, i) => {
            const d = drafts[i] ?? initialDraft(q);
            return (
              <View key={i}>
                <Text style={s.question}>
                  {q.question}
                  {q.multi_select ? <Text style={s.muted}>（可多选）</Text> : null}
                </Text>
                {q.options.map((o) => {
                  const on = d.selected.includes(o.label);
                  return (
                    <Pressable
                      key={o.label}
                      style={[s.opt, on && s.optOn]}
                      onPress={() =>
                        q.multi_select
                          ? setDraft(i, { selected: on ? d.selected.filter((x) => x !== o.label) : [...d.selected, o.label] })
                          : // 单选：选了选项就清掉「其他」，二者只能有一个（服务端同样校验）
                            setDraft(i, { selected: [o.label], other: "" })
                      }
                    >
                      {q.multi_select ? (
                        <View style={[s.cb, on && s.cbOn]}>{on && <Icon name="check" size={12} color="#fff" strokeWidth={3} />}</View>
                      ) : (
                        <View style={[s.rd, on && s.rdOn]} />
                      )}
                      <View style={{ flex: 1 }}>
                        <Text style={s.optText}>
                          {o.label}
                          {o.recommended ? <Text style={{ color: C.primaryInk, fontSize: F.xs }}>  推荐</Text> : null}
                        </Text>
                        {o.description ? <Text style={s.muted}>{o.description}</Text> : null}
                      </View>
                    </Pressable>
                  );
                })}
                <TextInput
                  style={s.other}
                  placeholder="其他（自己写）"
                  placeholderTextColor={C.ink4}
                  maxLength={500}
                  value={d.other}
                  onChangeText={(t) => setDraft(i, q.multi_select || !t ? { other: t } : { other: t, selected: [] })}
                />
              </View>
            );
          })}
          <View style={s.btns}>
            <Button style={{ flex: 1 }} loading={decide.isPending} onPress={() => submit(true)}>
              跳过，你来定
            </Button>
            <Button kind="primary" style={{ flex: 1 }} loading={decide.isPending} onPress={() => submit(false)}>
              提交
            </Button>
          </View>
        </>
      ) : (
        questions.map((q, i) => (
          <Text key={i} style={s.text}>
            {q.question}
          </Text>
        ))
      )}
      {decide.isError && !submitted ? <Text style={s.error}>提交失败：{decide.error.message}</Text> : null}
    </View>
  );
}

function answerText(a: QuestionAnswerData): string {
  if (a.by === "skipped") return "交给智能体决定";
  const parts = [...a.selected, ...(a.other ? [`其他：${a.other}`] : [])].join("、");
  return a.by === "recommended" ? `${parts}（按推荐）` : parts;
}

// ── 子智能体

const SUB_STATUS: Record<SubagentRun["status"], { tone: Tone; label: string }> = {
  running: { tone: "primary", label: "执行中" },
  ok: { tone: "success", label: "已完成" },
  error: { tone: "danger", label: "失败" },
};

export function SubAgentCard({ run, sub, subId, root }: { run: SubagentRun; sub?: RunState; subId?: string; root: RunState }) {
  const { openSubRun } = useChat();
  const [briefOpen, setBriefOpen] = useState(false);
  const status = SUB_STATUS[run.status];
  const steps = sub ? sub.toolCalls.length : 0;
  const hasProcess = (sub?.timeline.length ?? 0) > 0;
  const spoke = sub?.timeline.some((t) => t.kind === "text") ?? false;
  return (
    <View style={s.card}>
      <View style={s.cardHead}>
        <Avatar name={run.name} color="#d97706" size={24} square />
        <Text style={s.cardTitle} numberOfLines={1}>
          {run.name}
        </Text>
        <Text style={s.muted}>子智能体</Text>
        <View style={{ marginLeft: "auto" }}>
          <Tag tone={status.tone}>
            {run.status === "running" ? status.label : `${status.label} · ${durationMs(run.durationMs)}`}
          </Tag>
        </View>
      </View>
      {run.title && run.title !== run.task ? <Text style={[s.text, { fontWeight: "600" }]}>{run.title}</Text> : null}
      {(run.task || !hasProcess) && (
        <Pressable onPress={() => setBriefOpen((o) => !o)}>
          <Text style={s.label}>委派任务</Text>
          <Text style={s.text} numberOfLines={briefOpen ? undefined : 2}>
            {run.task || "（未提供任务描述）"}
          </Text>
        </Pressable>
      )}
      {hasProcess && (
        <Pressable style={s.linkRow} onPress={() => openSubRun(run, subId, root)}>
          {run.status === "running" ? <Spin /> : <Icon name="list" size={18} />}
          <Text style={{ flex: 1, fontSize: F.md, color: C.ink2 }}>
            过程{steps ? ` · ${steps} 个步骤` : ""}
          </Text>
          <Icon name="right" size={18} color={C.ink4} />
        </Pressable>
      )}
      {sub?.status === "failed" && sub.error ? <Text style={s.error}>没有完成：{sub.error.message}</Text> : null}
      {sub?.status === "cancelled" ? <Text style={s.muted}>已取消</Text> : null}
      {run.result && !spoke ? (
        <>
          <Text style={s.label}>返回结果</Text>
          <Text style={s.text} numberOfLines={6}>
            {run.result}
          </Text>
        </>
      ) : null}
    </View>
  );
}

// ── 执行计划

export function TodoCard({ todos }: { todos: Todo[] }) {
  if (todos.length === 0) return null;
  const done = todos.filter((t) => t.status === "completed").length;
  return (
    <View style={s.card}>
      <View style={s.cardHead}>
        <Icon name="todo" size={18} />
        <Text style={s.cardTitle}>执行计划</Text>
        <View style={{ marginLeft: "auto" }}>
          <Tag>{`${done}/${todos.length}`}</Tag>
        </View>
      </View>
      {todos.map((t, i) => {
        const isDone = t.status === "completed";
        const doing = t.status === "in_progress";
        return (
          <View key={t.id ?? i} style={s.todo}>
            <View style={[s.todoBox, isDone && s.todoBoxDone, doing && { borderWidth: 0 }]}>
              {isDone ? <Icon name="check" size={13} color="#fff" strokeWidth={3} /> : doing ? <Spin /> : null}
            </View>
            <Text
              style={[
                s.todoText,
                isDone && { color: C.ink4, textDecorationLine: "line-through" },
                doing && { color: C.ink, fontWeight: "600" },
              ]}
            >
              {t.content}
            </Text>
          </View>
        );
      })}
    </View>
  );
}

// ── 上下文压缩（系统行为，视觉上弱于卡片）

export function CompactionDivider({ mark }: { mark: CompactionMark }) {
  const [open, setOpen] = useState(false);
  let text: string;
  if (mark.variant === "cleared") {
    text = `${mark.reason === "idle" ? "距上次回复较久，" : "上下文较满，"}已清除 ${mark.cleared ?? 0} 个较早的工具结果（约 ${compactNumber(mark.tokensFreed ?? 0)} tokens）`;
  } else if (mark.degraded) {
    text = `早期 ${mark.messagesSummarized} 条消息已截断（摘要生成失败）`;
  } else {
    text = `${mark.reason === "overflow" ? "上下文超出模型上限，" : "上下文较满，"}早期 ${mark.messagesSummarized} 条消息已压缩成摘要`;
  }
  const hasSummary = mark.variant !== "cleared";
  return (
    <View style={s.divider}>
      <View style={s.dividerLine} />
      <Text style={s.dividerText}>
        {text}
        {hasSummary ? (
          <Text style={{ color: C.primary, fontWeight: "700" }} onPress={() => setOpen(true)}>
            {"  查看摘要"}
          </Text>
        ) : null}
      </Text>
      <View style={s.dividerLine} />
      {open && (
        <Sheet open onClose={() => setOpen(false)} title="早期对话的摘要" subtitle={`${mark.messagesSummarized} 条消息 · 原文仍完整保留，摘要只影响模型看到的内容`}>
          <View style={{ paddingHorizontal: 20, paddingBottom: 16 }}>
            <Text selectable style={[s.text, { lineHeight: 23 }]}>
              {mark.summary || "（无摘要内容）"}
            </Text>
          </View>
        </Sheet>
      )}
    </View>
  );
}

// ── 一行式提示：技能、MCP、知识库、CLI 权限模式

const DRIFT_TEXT: Record<string, string> = {
  used: "定义与保存时不一致，已照常使用新定义",
  blocked: "定义与保存时不一致，本轮未装载",
  pending_review: "定义尚未复核，本轮未装载",
  rejected: "定义复核未通过，本轮未装载",
};

function Line({ icon, children, tone }: { icon: IconName; children: ReactNode; tone?: "warn" }) {
  return (
    <View style={s.line}>
      <Icon name={icon} size={14} color={tone ? C.warning : C.ink3} />
      <Text style={[s.lineText, tone && { color: C.warningInk }]}>{children}</Text>
    </View>
  );
}

export function NoticeRow({ notice }: { notice: Notice }) {
  switch (notice.type) {
    case "skill.loaded":
      return <Line icon="spark">{`加载技能 ${notice.data.slug} v${notice.data.version}`}</Line>;
    case "skill.skipped":
      return (
        <Line icon="warn" tone="warn">
          {`技能 ${notice.data.slug} v${notice.data.version} ${notice.data.reason === "revoked" ? "已被紧急下架，本轮未装载" : `未装载（${notice.data.reason}）`}`}
        </Line>
      );
    case "knowledge.skipped": {
      const [what, skipped] = notice.data.set_ids ? ["知识集", notice.data.set_ids] : ["知识库", notice.data.dataset_ids ?? []];
      return (
        <Line icon="book" tone="warn">
          {`${skipped.length > 0 ? `${what}跳过 ${skipped.length} 个：` : ""}${notice.data.message}`}
        </Line>
      );
    }
    case "mcp.tool_skipped":
      return <Line icon="plug" tone="warn">{`MCP ${notice.data.server}：${notice.data.message}`}</Line>;
    case "mcp.tool_drift":
      return (
        <Line icon="plug" tone={notice.data.action === "used" ? undefined : "warn"}>
          {`MCP ${notice.data.tool}：${DRIFT_TEXT[notice.data.action] ?? notice.data.action}`}
        </Line>
      );
  }
}

const CLI_MODE: Record<string, string> = { manual: "手动确认", accept_edits: "自动接受编辑", auto: "Auto", plan: "仅规划" };
const cliMode = (m: string | null) => (m ? CLI_MODE[m] ?? m : "不支持");

export function ModeBadge({ mode }: { mode: AgentModeData }) {
  if (mode.source === "plan_approved") {
    return <Line icon="check">{`计划已批准，以「${APPROVAL_MODE_LABEL[mode.effective as ApprovalMode] ?? mode.effective}」模式执行`}</Line>;
  }
  if (!mode.degraded) return <Line icon="shield">{`CLI 权限模式：${cliMode(mode.effective)}`}</Line>;
  return (
    <Line icon="warn" tone="warn">
      {`CLI 权限模式：${cliMode(mode.requested)} → ${cliMode(mode.effective)}，${mode.effective ? "要求的模式未能生效" : "CLI 不支持权限模式"}`}
    </Line>
  );
}

const s = StyleSheet.create({
  card: { borderWidth: 1, borderColor: C.line, borderRadius: R.lg, padding: 14, marginBottom: 12, backgroundColor: C.surface },
  cardWarn: { borderColor: C.warnCardLine, backgroundColor: C.warnCardBg },
  cardHead: { flexDirection: "row", alignItems: "center", gap: 8, marginBottom: 8 },
  cardTitle: { fontSize: 14.5, fontWeight: "700", color: C.ink, flexShrink: 1 },
  muted: { fontSize: 12.5, color: C.ink3, flexShrink: 1 },
  label: { fontSize: F.xs, color: C.ink3, marginTop: 10, marginBottom: 4 },
  text: { fontSize: F.md, color: C.ink, lineHeight: 21 },
  mono: { fontFamily: MONO, fontSize: 13, color: C.ink },
  row: { flexDirection: "row", alignItems: "center", flexWrap: "wrap" },
  code: {
    fontFamily: MONO,
    fontSize: 12.5,
    lineHeight: 19,
    color: C.ink,
    backgroundColor: "#f6f7fb",
    borderWidth: 1,
    borderColor: C.line2,
    borderRadius: 10,
    padding: 10,
    marginTop: 2,
  },
  link: { color: C.primary, fontSize: F.sm, marginTop: 10 },
  input: {
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: R.md,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: F.input,
    minHeight: 64,
    textAlignVertical: "top",
    backgroundColor: C.surface,
    color: C.ink,
  },
  btns: { flexDirection: "row", gap: 8, marginTop: 12 },
  hint: { fontSize: F.xs, color: C.ink4, textAlign: "center", marginTop: 8 },
  error: { fontSize: F.sm, color: C.danger, marginTop: 8 },
  segm: { flexDirection: "row", padding: 2, backgroundColor: C.hover, borderRadius: 10 },
  segmItem: { flex: 1, height: 34, borderRadius: 8, alignItems: "center", justifyContent: "center" },
  segmOn: { backgroundColor: C.surface, elevation: 1 },
  segmText: { fontSize: 13.5, color: C.ink3 },
  question: { fontSize: 14.5, fontWeight: "700", color: C.ink, marginTop: 12, marginBottom: 8 },
  opt: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    minHeight: 46,
    paddingHorizontal: 12,
    paddingVertical: 8,
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: R.md,
    marginBottom: 8,
    backgroundColor: C.surface,
  },
  optOn: { borderColor: C.primary2, backgroundColor: C.primaryWeak },
  optText: { fontSize: 14.5, color: C.ink },
  rd: { width: 18, height: 18, borderRadius: 9, borderWidth: 2, borderColor: "#cbd0da" },
  rdOn: { borderWidth: 5, borderColor: C.primary },
  cb: { width: 18, height: 18, borderRadius: 5, borderWidth: 2, borderColor: "#cbd0da", alignItems: "center", justifyContent: "center" },
  cbOn: { backgroundColor: C.primary, borderColor: C.primary },
  other: { height: 44, borderWidth: 1, borderColor: C.line, borderRadius: R.md, paddingHorizontal: 12, fontSize: F.input, color: C.ink },
  linkRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    minHeight: 44,
    marginTop: 8,
    paddingHorizontal: 8,
    borderRadius: 10,
    backgroundColor: C.hover,
  },
  todo: { flexDirection: "row", alignItems: "center", gap: 10, minHeight: 34 },
  todoBox: { width: 20, height: 20, borderRadius: 6, borderWidth: 1.5, borderColor: "#cbd0da", alignItems: "center", justifyContent: "center" },
  todoBoxDone: { backgroundColor: C.success, borderColor: C.success },
  todoText: { flex: 1, fontSize: F.md, color: C.ink2 },
  divider: { flexDirection: "row", alignItems: "center", gap: 10, marginBottom: 18, marginTop: 4 },
  dividerLine: { flex: 1, height: StyleSheet.hairlineWidth, backgroundColor: C.line },
  dividerText: { fontSize: 12.5, color: C.ink4, maxWidth: "80%", textAlign: "center" },
  line: { flexDirection: "row", alignItems: "center", gap: 6, marginBottom: 10 },
  lineText: { flex: 1, fontSize: 12.5, color: C.ink3 },
});
