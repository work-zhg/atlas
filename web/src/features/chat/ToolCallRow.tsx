"use client";

import { Icon } from "@/components/Icon";
import { durationMs } from "@/lib/format";
import type { ToolCall } from "@/lib/run-reducer";
import styles from "./chat.module.css";

function preview(call: ToolCall): string {
  if (call.argsPreview) return call.argsPreview;
  if (call.args === undefined) return "";
  try {
    return JSON.stringify(call.args).slice(0, 200);
  } catch {
    return "";
  }
}

/** 被 CLI 的 auto 模式判定为有风险而拦下：不是工具坏了，用户也无从批准（设计 D8-A） */
const isAutoDenied = (call: ToolCall) => call.errorKind === "auto_mode_denied";

function detail(call: ToolCall): string {
  if (call.status === "error") return call.error ?? call.resultPreview ?? "（无错误详情）";
  if (call.resultPreview) return call.resultPreview;
  if (call.result !== undefined) {
    try {
      return JSON.stringify(call.result, null, 2);
    } catch {
      return String(call.result);
    }
  }
  return call.status === "running" ? "执行中…" : "（无返回内容）";
}

/** 可折叠的工具调用行。收起显示摘要参数，展开显示完整返回。 */
export function ToolCallRow({ call }: { call: ToolCall }) {
  const denied = isAutoDenied(call);
  const dotClass =
    call.status === "ok"
      ? styles.dotOk
      : denied
        ? styles.dotWarn
        : call.status === "error"
          ? styles.dotErr
          : styles.dotRun;

  return (
    <details className={styles.tool} style={call.depth > 0 ? { marginLeft: 16 } : undefined}>
      <summary>
        <Icon name="right" size={12} className={styles.caret} />
        <span className={`${styles.statusDot} ${dotClass}`} aria-hidden="true" />
        <span className={styles.toolName}>{call.name}</span>
        <span className={styles.toolArg}>{preview(call)}</span>
        {denied && (
          <span className={styles.toolDenied} title="CLI 的 Auto 模式判定此操作有风险，已拒绝执行">
            被 Auto 模式拦截{call.deniedReason ? `：${call.deniedReason}` : ""}
          </span>
        )}
        {call.fallbackApplied && (
          <span className={styles.toolCost} style={{ color: "var(--warning)" }}>
            已降级
          </span>
        )}
        <span className={styles.toolCost}>
          {call.status === "running" ? "运行中" : durationMs(call.durationMs)}
        </span>
      </summary>
      <div className={styles.toolBody}>{detail(call)}</div>
    </details>
  );
}
