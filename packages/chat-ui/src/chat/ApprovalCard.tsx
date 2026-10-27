"use client";

import { Alert, Button, Input, Tag } from "antd";
import { useState } from "react";

import { useApprovalStillPending, useDecideApproval } from "../api/runs";
import { Markdown } from "../components/Markdown";
import { APPROVAL_MODE_LABEL, type ApprovalMode } from "../lib/events";
import { ApiError } from "../lib/http";
import type { PendingApproval } from "../lib/run-reducer";
import styles from "./chat.module.css";

interface Props {
  approval: PendingApproval;
  /** run 还在等它（reducer 的 pendingApprovals 里有它）。run 走到终态后为 false */
  waiting: boolean;
}

type Decision = "approved" | "rejected";
type ExecutionMode = Exclude<ApprovalMode, "plan">;

interface Decided {
  decision: Decision;
  /** 计划获批时选的执行模式 */
  mode?: ExecutionMode;
}

/**
 * 对话里的审批卡片：工具调用的确认，或规划模式提交的计划（doc/native-approval-mode-design.html）。
 *
 * ★ 在对话流里，不弹窗：审批发生在哪一步，卡片就出现在哪一步 —— 用户能看到
 *   它前面模型说了什么、在委派谁，判断才有依据。弹窗把它从上下文里摘出来，
 *   还挡住了下面正在发生的事。
 * ★ 没有等待上限：在用户做决定之前一直停在这里，系统不会替用户按拒绝处理。
 * ★ 处理完不消失：卡片留在原位显示结果，对话才是完整的记录。
 * ★ 展示调用参数（设计 §8）：用户只看工具名没法判断批不批 —— 派活要看到完整任务书，
 *   执行命令要看到命令本身。长内容（写文件的整段内容）折叠起来，不把对话撑长。
 */
export function ApprovalCard({ approval, waiting }: Props) {
  const decide = useDecideApproval();
  const [decided, setDecided] = useState<Decided | null>(null);
  const [feedback, setFeedback] = useState("");
  // ★ 以库为准：事件回放只说明「曾经要求过审批」。刷新后首连回放会把早已处理
  //   过的 approval.required 再送来一遍，只凭它就会出现一张还能点的旧卡片。
  const stillPending = useApprovalStillPending(
    waiting && !decided ? approval.runId : undefined,
    approval.approvalId,
  );
  // 查库失败时宁可让用户能点：藏掉一个真在等的审批，run 就一直停在那里
  const canDecide =
    waiting && !decided && (stillPending.data === true || stillPending.isError);
  const closedElsewhere = waiting && !decided && stillPending.isSuccess && !stillPending.data;
  const isPlan = approval.kind === "plan";

  const submit = (decision: Decision, mode?: ExecutionMode) => {
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
          // 409 = 已在别处处理过（另一个标签页、或这一轮已经结束）
          if (err instanceof ApiError && err.status === 409) void stillPending.refetch();
        },
      },
    );
  };

  let status: { color: string; label: string };
  if (decided?.decision === "approved") {
    status = {
      color: "success",
      label: decided.mode ? `已批准 · ${APPROVAL_MODE_LABEL[decided.mode]}执行` : "已批准",
    };
  } else if (decided) status = { color: "error", label: isPlan ? "已退回修改" : "已拒绝" };
  else if (canDecide) status = { color: "warning", label: "等待你确认" };
  else if (closedElsewhere) status = { color: "default", label: "已处理" };
  else if (!waiting) status = { color: "default", label: "已结束" };
  else status = { color: "default", label: "核对中…" };

  return (
    <div className={`${styles.approval} ${canDecide ? styles.approvalActive : ""}`}>
      <div className={styles.approvalHead}>
        <span className={styles.saName}>{isPlan ? "计划待批准" : "需要你确认"}</span>
        {!isPlan && <code className={styles.toolName}>{approval.toolName}</code>}
        <Tag color={status.color} style={{ marginInlineEnd: 0, marginLeft: "auto" }}>
          {status.label}
        </Tag>
      </div>

      <div className={styles.saBody}>
        {approval.reason && !isPlan && <p className={styles.saText}>{approval.reason}</p>}
        {isPlan ? <PlanView args={approval.args} /> : <ArgsView name={approval.toolName} args={approval.args} />}

        {canDecide && approval.acceptsFeedback && (
          <Input.TextArea
            className={styles.approvalFeedback}
            autoSize={{ minRows: 1, maxRows: 4 }}
            maxLength={4000}
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            placeholder={
              isPlan
                ? "修改意见（可选）：退回时交给智能体，它会继续完善计划"
                : "拒绝原因（可选）：会告诉智能体为什么不行"
            }
          />
        )}

        {canDecide && (
          <div className={styles.approvalActions}>
            <span className={styles.approvalHint}>
              {isPlan
                ? "批准后按所选模式在这一轮继续执行"
                : "拒绝不会中断这轮对话，智能体会换一种方式继续"}
            </span>
            <Button danger loading={decide.isPending} onClick={() => submit("rejected")}>
              {isPlan ? (feedback.trim() ? "提修改意见" : "不批准") : "拒绝"}
            </Button>
            {isPlan ? (
              approval.executionModes.map((mode) => (
                <Button
                  key={mode}
                  // 按严格程度排（手动在前），最右边那个是主按钮 —— antd 惯例
                  type={mode === approval.executionModes.at(-1) ? "primary" : "default"}
                  loading={decide.isPending}
                  onClick={() => submit("approved", mode as ExecutionMode)}
                >
                  批准，{APPROVAL_MODE_LABEL[mode]}执行
                </Button>
              ))
            ) : (
              <Button type="primary" loading={decide.isPending} onClick={() => submit("approved")}>
                批准执行
              </Button>
            )}
          </div>
        )}

        {decide.isError && !decided && (
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

function asRecord(args: unknown): Record<string, unknown> {
  return args && typeof args === "object" ? (args as Record<string, unknown>) : {};
}

function text(a: Record<string, unknown>, key: string): string | undefined {
  const v = a[key];
  return typeof v === "string" ? v : undefined;
}

/** 规划模式提交的计划（exit_plan_mode 的 plan 参数，Markdown） */
function PlanView({ args }: { args: unknown }) {
  const plan = text(asRecord(args), "plan") ?? "";
  return <Markdown className={`${styles.prose} ${styles.approvalPlan}`}>{plan}</Markdown>;
}

/**
 * 按工具渲染调用参数。工具名就是 kernel 注册的名字（kernel/middleware 与 sandbox）；
 * 认不出的工具（MCP、acp 的 CLI 工具）显示格式化的 JSON。
 */
function ArgsView({ name, args }: { name: string; args: unknown }) {
  const a = asRecord(args);
  const path = text(a, "file_path");
  switch (name) {
    case "execute": {
      const command = text(a, "command");
      if (command === undefined) break;
      return <pre className={styles.approvalCode}>{command}</pre>;
    }
    case "write_file": {
      const content = text(a, "content");
      if (path === undefined || content === undefined) break;
      return (
        <details className={styles.approvalArgs}>
          <summary>
            <code>{path}</code> · {content.length.toLocaleString()} 字符（展开查看内容）
          </summary>
          <pre className={styles.approvalCode}>{content}</pre>
        </details>
      );
    }
    case "edit_file": {
      const before = text(a, "old_string");
      const after = text(a, "new_string");
      if (path === undefined || before === undefined || after === undefined) break;
      return (
        <div className={styles.approvalArgs}>
          <code>{path}</code>
          {a.replace_all === true && <Tag style={{ marginLeft: 6 }}>全部替换</Tag>}
          <p className={styles.saLabel}>修改前</p>
          <pre className={`${styles.approvalCode} ${styles.approvalBefore}`}>{before}</pre>
          <p className={styles.saLabel}>修改后</p>
          <pre className={`${styles.approvalCode} ${styles.approvalAfter}`}>{after}</pre>
        </div>
      );
    }
    case "delete": {
      if (path === undefined) break;
      return (
        <p className={styles.approvalArgs}>
          删除 <code>{path}</code>
        </p>
      );
    }
    case "task": {
      const brief = text(a, "prompt");
      if (brief === undefined) break;
      return (
        <div className={styles.approvalArgs}>
          <p className={styles.saLabel}>委派给</p>
          <Tag style={{ marginInlineEnd: 6 }}>{text(a, "subagent_type")}</Tag>
          {text(a, "description")}
          <p className={styles.saLabel}>任务书</p>
          <pre className={styles.approvalCode}>{brief}</pre>
        </div>
      );
    }
  }
  return <pre className={styles.approvalCode}>{JSON.stringify(args, null, 2)}</pre>;
}
