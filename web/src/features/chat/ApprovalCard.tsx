"use client";

import { Alert, Button, Tag } from "antd";
import { useState } from "react";

import { useApprovalStillPending, useDecideApproval } from "@/api/runs";
import { ApiError } from "@/lib/http";
import type { PendingApproval } from "@/lib/run-reducer";
import styles from "./chat.module.css";

interface Props {
  approval: PendingApproval;
  /** run 还在等它（reducer 的 pendingApprovals 里有它）。run 走到终态后为 false */
  waiting: boolean;
}

type Decision = "approved" | "rejected";

const DECIDED: Record<Decision, { color: string; label: string }> = {
  approved: { color: "success", label: "已批准" },
  rejected: { color: "error", label: "已拒绝" },
};

/**
 * 对话里的审批卡片（文档 §12.2）。
 *
 * ★ 在对话流里，不弹窗：审批发生在哪一步，卡片就出现在哪一步 —— 用户能看到
 *   它前面模型说了什么、在委派谁，判断才有依据。弹窗把它从上下文里摘出来，
 *   还挡住了下面正在发生的事。
 * ★ 没有等待上限：在用户做决定之前一直停在这里，系统不会替用户按拒绝处理。
 * ★ 处理完不消失：卡片留在原位显示结果，对话才是完整的记录。
 * ★ 不展示调用参数：只给工具名与原因。参数（尤其是写文件的整段内容）会把对话撑得很长。
 */
export function ApprovalCard({ approval, waiting }: Props) {
  const decide = useDecideApproval();
  const [decided, setDecided] = useState<Decision | null>(null);
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

  const submit = (decision: Decision) => {
    if (!approval.runId) return;
    decide.mutate(
      { runId: approval.runId, approvalId: approval.approvalId, decision },
      {
        // ★ 只有提交成功才算已决策：网络失败时按钮要留着，用户才能重试
        onSuccess: () => setDecided(decision),
        onError: (err) => {
          // 409 = 已在别处处理过（另一个标签页、或这一轮已经结束）
          if (err instanceof ApiError && err.status === 409) void stillPending.refetch();
        },
      },
    );
  };

  let status: { color: string; label: string };
  if (decided) status = DECIDED[decided];
  else if (canDecide) status = { color: "warning", label: "等待你确认" };
  else if (closedElsewhere) status = { color: "default", label: "已处理" };
  else if (!waiting) status = { color: "default", label: "已结束" };
  else status = { color: "default", label: "核对中…" };

  return (
    <div className={`${styles.approval} ${canDecide ? styles.approvalActive : ""}`}>
      <div className={styles.approvalHead}>
        <span className={styles.saName}>需要你确认</span>
        <code className={styles.toolName}>{approval.toolName}</code>
        <Tag color={status.color} style={{ marginInlineEnd: 0, marginLeft: "auto" }}>
          {status.label}
        </Tag>
      </div>

      {/* 没有原因、也不用决定时，卡片只剩标题一行 —— 不留一块空白的正文区 */}
      {(approval.reason || canDecide || (decide.isError && !decided)) && (
        <div className={styles.saBody}>
          {approval.reason && <p className={styles.saText}>{approval.reason}</p>}
  
          {canDecide && (
            <div className={styles.approvalActions}>
              <span className={styles.approvalHint}>拒绝不会中断这轮对话，智能体会换一种方式继续</span>
              <Button danger loading={decide.isPending} onClick={() => submit("rejected")}>
                拒绝
              </Button>
              <Button type="primary" loading={decide.isPending} onClick={() => submit("approved")}>
                批准执行
              </Button>
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
      )}
    </div>
  );
}
