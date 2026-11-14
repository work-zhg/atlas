"use client";

import { Alert, Button, Input, Tag } from "antd";
import { useState } from "react";

import {
  useDeclineTakeover,
  useHandBackTakeover,
  useOpenTakeover,
  type TakeoverLogin,
} from "../api/browser";
import { useApprovalStillPending } from "../api/runs";
import type { TakeoverArgs, TakeoverSettledData } from "../lib/events";
import { ApiError } from "../lib/http";
import type { PendingApproval } from "../lib/run-reducer";
import { BrowserView } from "./BrowserView";
import styles from "./chat.module.css";

interface Props {
  approval: PendingApproval;
  /** run 还在等它（reducer 的 pendingApprovals 里有它）。处理后或 run 走到终态后为 false */
  waiting: boolean;
  /** takeover.settled 带来的结果（进轨迹，刷新后也在） */
  settled?: TakeoverSettledData;
}

interface Done {
  decision: "approved" | "rejected";
  login?: TakeoverLogin | null;
}

/**
 * 请人接管浏览器（c4-model/agent/browser/code-browser-takeover.html）：智能体遇到登录、验证码停下，
 * 人在实时画面里自己操作，操作完点「交还」，智能体接着做。
 *
 * ★ 和审批卡片同一套骨架：在对话流里、出现在发生的那一步；没有等待上限（人可以第二天再来）。
 * ★ 先「打开」：确保浏览器在（被闲置回收了要重建，几十秒）、开一个专用窗口，再接上画面。
 *   不碰智能体正在用的页面；登录写进的是整个浏览器共用的 cookie。
 * ★ 输入从你的键盘经画面直达浏览器，不进智能体的上下文。只有会话的创建人能处理。
 */
export function TakeoverCard({ approval, waiting, settled }: Props) {
  const args = (approval.args ?? {}) as Partial<TakeoverArgs>;
  const open = useOpenTakeover();
  const handBack = useHandBackTakeover();
  const decline = useDeclineTakeover();
  const [threadId, setThreadId] = useState<string>();
  const [note, setNote] = useState("");
  const [done, setDone] = useState<Done | null>(null);
  // ★ 以库为准（同审批卡片）：刷新后首连回放会把早已处理过的 approval.required 再送来一遍
  const stillPending = useApprovalStillPending(
    waiting && !done && !settled ? approval.runId : undefined,
    approval.approvalId,
  );
  const canAct =
    waiting && !done && !settled && (stillPending.data === true || stillPending.isError);
  const closedElsewhere = waiting && !done && !settled && stillPending.isSuccess && !stillPending.data;
  const busy = open.isPending || handBack.isPending || decline.isPending;
  const error = open.error ?? handBack.error ?? decline.error;

  const target = approval.runId ? { runId: approval.runId, approvalId: approval.approvalId } : null;
  const onConflict = (err: Error) => {
    // 409 = 已在别处处理过（另一个标签页、或这一轮已经结束）
    if (err instanceof ApiError && err.status === 409) void stillPending.refetch();
  };

  const doOpen = () => {
    if (!target) return;
    open.mutate(target, { onSuccess: (r) => setThreadId(r.thread_id), onError: onConflict });
  };
  const doHandBack = () => {
    if (!target) return;
    handBack.mutate(
      { ...target, note },
      { onSuccess: (r) => setDone({ decision: "approved", login: r.login }), onError: onConflict },
    );
  };
  const doDecline = () => {
    if (!target) return;
    decline.mutate(
      { ...target, note },
      { onSuccess: () => setDone({ decision: "rejected" }), onError: onConflict },
    );
  };

  const result = settled
    ? { decision: settled.decision, login: settled.login, note: settled.note }
    : done
      ? { decision: done.decision, login: done.login, note: note.trim() || undefined }
      : null;

  let status: { color: string; label: string };
  if (result?.decision === "approved") status = { color: "success", label: "已交还" };
  else if (result?.decision === "rejected") status = { color: "default", label: "没有处理" };
  else if (result) status = { color: "default", label: "已失效" };
  else if (canAct) status = { color: "warning", label: threadId ? "请在画面里操作" : "等待你接管" };
  else if (closedElsewhere) status = { color: "default", label: "已处理" };
  else if (!waiting) status = { color: "default", label: "已结束" };
  else status = { color: "default", label: "核对中…" };

  return (
    <div className={`${styles.approval} ${canAct ? styles.approvalActive : ""}`}>
      <div className={styles.approvalHead}>
        <span className={styles.saName}>需要你接管浏览器</span>
        <Tag color={status.color} style={{ marginInlineEnd: 0, marginLeft: "auto" }}>
          {status.label}
        </Tag>
      </div>

      <div className={styles.saBody}>
        <p className={styles.saText}>{args.reason || "智能体需要你在浏览器里操作一下"}</p>
        {args.url && <p className={styles.takeoverUrl}>{args.url}</p>}

        {result ? (
          <p className={styles.saText} style={{ marginTop: 8 }}>
            {resultText(result)}
          </p>
        ) : canAct ? (
          <>
            {threadId && <BrowserView threadId={threadId} interactive />}
            {threadId && (
              <Input.TextArea
                className={styles.approvalFeedback}
                autoSize={{ minRows: 1, maxRows: 3 }}
                maxLength={500}
                placeholder="（可选）给智能体留一句话，如「登录好了，验证码已经输过」"
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
            )}
            <div className={styles.approvalActions}>
              <span className={styles.approvalHint}>
                {threadId
                  ? "在上面的画面里操作；做完点「交还」，智能体接着做"
                  : "打开后在画面里自己登录：密码、验证码不经过智能体"}
              </span>
              <Button loading={decline.isPending} disabled={busy} onClick={doDecline}>
                不处理
              </Button>
              {threadId ? (
                <Button type="primary" loading={handBack.isPending} disabled={busy} onClick={doHandBack}>
                  交还
                </Button>
              ) : (
                <Button type="primary" loading={open.isPending} disabled={busy} onClick={doOpen}>
                  打开
                </Button>
              )}
            </div>
          </>
        ) : null}

        {error && !result && (
          <Alert
            type="error"
            showIcon
            style={{ marginTop: 12 }}
            message={open.error ? "打不开接管" : "提交失败"}
            description={error.message}
          />
        )}
      </div>
    </div>
  );
}

function resultText(r: { decision: string; login?: TakeoverLogin | null; note?: string }): string {
  if (r.decision === "approved") {
    const saved = r.login?.saved === false
      ? "，登录态没能存下来（下次可能要再登录）"
      : r.login?.sites?.length
        ? `，已记住 ${r.login.sites.join("、")} 的登录`
        : "";
    return `已交还，智能体接着做${saved}${r.note ? `。你的说明：${r.note}` : ""}`;
  }
  if (r.decision === "rejected") return `没有处理${r.note ? `：${r.note}` : ""}`;
  return "接管已失效（这一轮已结束）";
}
