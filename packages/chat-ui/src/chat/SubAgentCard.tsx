"use client";

import { Alert, Tag } from "antd";
import type { ReactNode } from "react";

import { Icon } from "../components/Icon";
import { durationMs } from "../lib/format";
import type { RunState, SubagentRun } from "../lib/run-reducer";
import { avatarBackground, avatarLetter } from "../lib/avatar";
import styles from "./chat.module.css";

const STATUS: Record<SubagentRun["status"], { color: string; label: string }> = {
  running: { color: "processing", label: "执行中" },
  ok: { color: "success", label: "已完成" },
  error: { color: "error", label: "失败" },
};

/**
 * 子智能体委派块：任务书 → 过程 → 结果。
 *
 * ★ 过程是子 run 自己的时间线（children，由调用方渲染）：它说的话、工具、待办、
 *   技能提示、审批、它再委派的 SubAgent，与主对话同样呈现。卡片与子 run 的对应来自
 *   父 run 的 run.suspended（waiting_on 里 tool_call_id → 子 run id），并发委派也不会串。
 * ★ 子 run 说过话就不再单列「返回结果」：结论就是它最后那段话
 *   （server 的 format_subagent_result），再贴一遍只是重复。没说话时
 *   （没给结论、或 acp 子 run 的历史轮次 —— delta 不归档）还靠结果。
 */
export function SubAgentCard({
  run,
  sub,
  children,
}: {
  run: SubagentRun;
  /** 子 run 的状态；没有 = 看不到过程（归档已清理、事件不在回放窗口里） */
  sub?: RunState;
  children?: ReactNode;
}) {
  const status = STATUS[run.status];
  const hasProcess = (sub?.timeline.length ?? 0) > 0;
  const spoke = sub?.timeline.some((t) => t.kind === "text") ?? false;

  return (
    <details className={styles.subagent} open={run.status === "running"}>
      <summary>
        <Icon name="right" size={12} className={styles.caret} />
        <span
          className={styles.agentAvatar}
          style={{
            width: 18,
            height: 18,
            fontSize: 9,
            borderRadius: 4,
            background: avatarBackground("research"),
          }}
          aria-hidden="true"
        >
          {avatarLetter(run.name)}
        </span>
        <span className={styles.saName}>{run.name}</span>
        {/* 只给了标题的旧调用：task 就是标题，别重复显示 */}
        {run.title && run.title !== run.task && (
          <span className={styles.saTitle} title={run.title}>
            {run.title}
          </span>
        )}
        <Tag color={status.color} style={{ marginInlineEnd: 0 }}>
          {status.label}
        </Tag>
        <span className={styles.toolCost} style={{ marginLeft: "auto" }}>
          {run.status === "running" ? "执行中" : durationMs(run.durationMs)}
        </span>
      </summary>

      <div className={styles.saBody}>
        {/* 有过程、没有任务书的（刷新后找不到派出它的那次调用）就不放一行空占位 */}
        {(run.task || !hasProcess) && (
          <>
            <p className={styles.saLabel}>委派任务</p>
            <p className={styles.saText}>{run.task || "（未提供任务描述）"}</p>
          </>
        )}

        {hasProcess && (
          <>
            <p className={styles.saLabel}>过程</p>
            <div className={styles.saProcess}>{children}</div>
          </>
        )}

        {sub?.status === "failed" && sub.error && (
          <Alert
            type="error"
            showIcon
            style={{ marginTop: "var(--s-3)" }}
            message={`运行失败（${sub.error.kind}）`}
            description={sub.error.message}
          />
        )}
        {sub?.status === "cancelled" && (
          <Alert type="info" showIcon style={{ marginTop: "var(--s-3)" }} message="已取消" />
        )}

        {run.result && !spoke && (
          <>
            <p className={styles.saLabel}>返回结果</p>
            <p className={styles.saText}>{run.result}</p>
          </>
        )}
      </div>
    </details>
  );
}
