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

function detail(call: ToolCall): string {
  if (call.status === "error") return call.error ?? "（无错误详情）";
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
  const dotClass =
    call.status === "ok"
      ? styles.dotOk
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
