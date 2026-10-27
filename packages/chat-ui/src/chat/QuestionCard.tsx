"use client";

import { Alert, Button, Checkbox, Input, Radio, Tag } from "antd";
import { useState } from "react";

import { useApprovalStillPending, useDecideApproval } from "../api/runs";
import type {
  AskUserQuestionArgs,
  QuestionAnswerData,
  QuestionAnsweredData,
  QuestionSpec,
} from "../lib/events";
import { ApiError } from "../lib/http";
import type { PendingApproval } from "../lib/run-reducer";
import styles from "./chat.module.css";

interface Props {
  approval: PendingApproval;
  /** run 还在等它（reducer 的 pendingApprovals 里有它）。回答后或 run 走到终态后为 false */
  waiting: boolean;
  /** question.answered 带来的答案（进轨迹，刷新后也在） */
  answered?: QuestionAnsweredData;
}

interface Draft {
  selected: string[];
  other: string;
}

/**
 * 智能体向用户提的问题（ask_user_question，doc/ask-user-question-design.html）。
 *
 * ★ 和审批卡片同一套骨架：在对话流里、出现在发生的那一步；没有等待上限；回答后留在原位显示答案。
 * ★ 推荐项预选：用户只改不同意的那几题，直接「提交回答」即可 —— 回答成本越低，用户越愿意答。
 * ★ 「跳过，你来定」：每题都记为跳过，智能体按推荐继续并写明假设。
 */
export function QuestionCard({ approval, waiting, answered }: Props) {
  const questions = (approval.args as AskUserQuestionArgs | undefined)?.questions ?? [];
  const decide = useDecideApproval();
  const [drafts, setDrafts] = useState<Draft[]>(() => questions.map(initialDraft));
  const [submitted, setSubmitted] = useState<QuestionAnswerData[] | null>(null);
  // ★ 以库为准（同审批卡片）：刷新后首连回放会把早已回答过的 approval.required 再送来一遍
  const stillPending = useApprovalStillPending(
    waiting && !submitted && !answered ? approval.runId : undefined,
    approval.approvalId,
  );
  const canAnswer =
    waiting && !submitted && !answered && (stillPending.data === true || stillPending.isError);
  const closedElsewhere =
    waiting && !submitted && !answered && stillPending.isSuccess && !stillPending.data;

  const setDraft = (index: number, patch: Partial<Draft>) =>
    setDrafts((all) => all.map((d, i) => (i === index ? { ...d, ...patch } : d)));

  const submit = (skip: boolean) => {
    if (!approval.runId) return;
    const answers = drafts.map((d) =>
      skip ? { selected: [], other: null } : { selected: d.selected, other: d.other.trim() || null },
    );
    decide.mutate(
      { runId: approval.runId, approvalId: approval.approvalId, decision: "approved", answers },
      {
        // ★ 只有提交成功才算已回答：网络失败时表单要留着，用户才能重试
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
          // 409 = 已在别处回答过（另一个标签页、或这一轮已经结束）
          if (err instanceof ApiError && err.status === 409) void stillPending.refetch();
        },
      },
    );
  };

  const shown = answered?.answers ?? submitted;
  let status: { color: string; label: string };
  if (answered || submitted) status = { color: "success", label: "已回答" };
  else if (canAnswer) status = { color: "warning", label: "等待你回答" };
  else if (closedElsewhere) status = { color: "default", label: "已处理" };
  else if (!waiting) status = { color: "default", label: "已结束" };
  else status = { color: "default", label: "核对中…" };

  return (
    <div className={`${styles.approval} ${canAnswer ? styles.approvalActive : ""}`}>
      <div className={styles.approvalHead}>
        <span className={styles.saName}>需要你回答</span>
        <Tag color={status.color} style={{ marginInlineEnd: 0, marginLeft: "auto" }}>
          {status.label}
        </Tag>
      </div>

      <div className={styles.saBody}>
        {shown ? (
          <AnswerList answers={shown} feedback={answered?.feedback} />
        ) : canAnswer ? (
          <>
            {questions.map((q, i) => (
              <QuestionForm
                key={i}
                spec={q}
                draft={drafts[i] ?? initialDraft(q)}
                onChange={(patch) => setDraft(i, patch)}
              />
            ))}
            <div className={styles.approvalActions}>
              <span className={styles.approvalHint}>回答后智能体在这一轮继续</span>
              <Button loading={decide.isPending} onClick={() => submit(true)}>
                跳过，你来定
              </Button>
              <Button type="primary" loading={decide.isPending} onClick={() => submit(false)}>
                提交回答
              </Button>
            </div>
          </>
        ) : (
          // 不在等了（已结束 / 在别处处理过）又没有答案：只列出问过什么
          questions.map((q, i) => (
            <p key={i} className={styles.questionTitle}>
              <span className={styles.questionChip}>{q.header}</span>
              {q.question}
            </p>
          ))
        )}

        {decide.isError && !submitted && (
          <Alert
            type="error"
            showIcon
            style={{ marginTop: 12 }}
            message="提交失败"
            description={decide.error.message}
          />
        )}
      </div>
    </div>
  );
}

/** 推荐项预选（多选题也预选推荐的那一个） */
function initialDraft(q: QuestionSpec): Draft {
  return { selected: q.options.filter((o) => o.recommended).map((o) => o.label), other: "" };
}

function QuestionForm({
  spec,
  draft,
  onChange,
}: {
  spec: QuestionSpec;
  draft: Draft;
  onChange: (patch: Partial<Draft>) => void;
}) {
  const options = spec.options.map((o) => ({
    value: o.label,
    label: (
      <span>
        {o.label}
        {o.recommended && (
          <Tag color="processing" style={{ marginLeft: 6 }}>
            推荐
          </Tag>
        )}
        {o.description && <span className={styles.questionDesc}>{o.description}</span>}
      </span>
    ),
  }));
  return (
    <div className={styles.questionItem}>
      <p className={styles.questionTitle}>
        <span className={styles.questionChip}>{spec.header}</span>
        {spec.question}
        {spec.multi_select && <span className={styles.questionDesc}>（可多选）</span>}
      </p>
      {spec.multi_select ? (
        <Checkbox.Group
          className={styles.questionOptions}
          value={draft.selected}
          options={options}
          onChange={(v) => onChange({ selected: v as string[] })}
        />
      ) : (
        <Radio.Group
          className={styles.questionOptions}
          value={draft.selected[0]}
          options={options}
          // 单选：选了选项就清掉「其他」，二者只能有一个（服务端同样校验）
          onChange={(e) => onChange({ selected: [e.target.value as string], other: "" })}
        />
      )}
      <Input
        size="small"
        className={styles.questionOther}
        placeholder="其他（自己填）"
        maxLength={500}
        value={draft.other}
        onChange={(e) =>
          onChange(
            spec.multi_select || !e.target.value
              ? { other: e.target.value }
              : { other: e.target.value, selected: [] },
          )
        }
      />
    </div>
  );
}

function AnswerList({
  answers,
  feedback,
}: {
  answers: QuestionAnswerData[];
  feedback?: string | null;
}) {
  if (answers.length === 0) {
    return (
      <p className={styles.saText}>
        {feedback ? `没有按选项回答：${feedback}` : "没有回答，交给智能体决定"}
      </p>
    );
  }
  return (
    <>
      {answers.map((a, i) => (
        <p key={i} className={styles.questionTitle}>
          <span className={styles.questionChip}>{a.header}</span>
          {answerText(a)}
        </p>
      ))}
    </>
  );
}

function answerText(a: QuestionAnswerData): string {
  if (a.by === "skipped") return "交给智能体决定";
  const parts = [...a.selected, ...(a.other ? [`其他：${a.other}`] : [])].join("、");
  return a.by === "recommended" ? `${parts}（按推荐）` : parts;
}
