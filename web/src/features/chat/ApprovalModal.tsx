"use client";

import { Alert, Button, Modal } from "antd";

import { useDecideApproval } from "@/api/runs";
import { ApiError } from "@/lib/http";
import type { PendingApproval } from "@/lib/run-reducer";
import styles from "./chat.module.css";

interface Props {
  runId: string | undefined;
  approval: PendingApproval | undefined;
  onDecided: (approvalId: string) => void;
}

/**
 * 高风险工具的确认弹窗（文档 §12.2）。
 *
 * 两个刻意的选择：
 *   · **展示完整参数**，不是摘要 —— 让用户看不到自己在批准什么，
 *     这个门禁就只是个多余的点击
 *   · **不提供「关闭」**（无 X、点遮罩不关）：run 正停在那里等，
 *     随手关掉只会让它一直挂到十分钟超时，用户还不知道发生了什么
 */
export function ApprovalModal({ runId, approval, onDecided }: Props) {
  const decide = useDecideApproval();

  const submit = (decision: "approved" | "rejected") => {
    if (!runId || !approval) return;
    decide.mutate(
      { runId, approvalId: approval.approvalId, decision },
      {
        // ★ 只有提交成功才算已决策。onSettled 会把网络失败也当成已处理：
        //   弹窗关闭、run 挂到十分钟超时，用户连下面的错误提示都看不到。
        onSuccess: () => onDecided(approval.approvalId),
        onError: (err) => {
          // 409 = 服务端已处理过（超时/别处已决策）—— 留着弹窗只会反复 409
          if (err instanceof ApiError && err.status === 409) onDecided(approval.approvalId);
        },
      },
    );
  };

  return (
    <Modal
      open={Boolean(approval)}
      title="需要你确认"
      closable={false}
      maskClosable={false}
      keyboard={false}
      footer={[
        <Button key="reject" danger loading={decide.isPending} onClick={() => submit("rejected")}>
          拒绝
        </Button>,
        <Button
          key="approve"
          type="primary"
          loading={decide.isPending}
          onClick={() => submit("approved")}
        >
          批准执行
        </Button>,
      ]}
    >
      {approval && (
        <>
          <p style={{ fontSize: 13, color: "var(--fg-2)", marginTop: 0 }}>
            智能体想执行 <code className={styles.toolName}>{approval.toolName}</code>
            {approval.reason && `（${approval.reason}）`}
          </p>

          <p className={styles.insSection}>调用参数</p>
          <pre className={styles.toolBody} style={{ margin: 0, maxHeight: 300 }}>
            {JSON.stringify(approval.args, null, 2)}
          </pre>

          <Alert
            type="info"
            showIcon
            style={{ marginTop: 12 }}
            message="拒绝不会中断这轮对话"
            description="智能体会收到「用户拒绝执行」，可以换一种方式继续。"
          />

          {decide.isError && (
            <Alert
              type="error"
              showIcon
              style={{ marginTop: 12 }}
              message="提交失败"
              description={decide.error.message}
            />
          )}
        </>
      )}
    </Modal>
  );
}
