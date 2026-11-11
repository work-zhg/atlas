"use client";

/**
 * 一组连续的工具调用，按「步骤」呈现：进行中显示「正在阅读 xxx…」，结束后收成「已完成 N 个步骤」。
 *
 * ★ 用人话描述动作（lib/steps.ts），不直接露工具名和 JSON；点开一步才看参数与结果。
 * ★ 进行中默认展开、结束自动收起 —— 用户手动点过就按用户的来，不再自动开合。
 */
import { useEffect, useRef, useState } from "react";

import { Icon } from "../components/Icon";
import type { ToolCall } from "../lib/run-reducer";
import { stepDuration, stepsTitle, stepView } from "../lib/steps";
import styles from "./chat.module.css";
import { MemoryNotice } from "./MemoryNotice";

/** 被 CLI 的 auto 模式判定为有风险而拦下：不是工具坏了，用户也无从批准（设计 D8-A） */
const isAutoDenied = (call: ToolCall) => call.errorKind === "auto_mode_denied";

function json(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

function detail(call: ToolCall): string {
  const parts: string[] = [];
  if (call.args !== undefined && !(call.args && typeof call.args === "object" && Object.keys(call.args).length === 0)) {
    parts.push(`参数\n${json(call.args)}`);
  } else if (call.argsPreview) {
    parts.push(`参数\n${call.argsPreview}`);
  }
  if (call.status === "error") {
    parts.push(`没有成功\n${call.error ?? call.resultPreview ?? "（没有说明原因）"}`);
  } else if (call.resultPreview) {
    parts.push(`结果\n${call.resultPreview}`);
  } else if (call.result !== undefined) {
    parts.push(`结果\n${json(call.result)}`);
  } else if (call.status === "running") {
    parts.push("进行中…");
  }
  return parts.join("\n\n") || "（没有更多信息）";
}

function Step({ call, live }: { call: ToolCall; live: boolean }) {
  const [open, setOpen] = useState(false);
  const v = stepView(call);
  const denied = isAutoDenied(call);
  const running = live && call.status === "running";
  const iconClass =
    call.status === "error" ? (denied ? styles.stepIconWarn : styles.stepIconErr) : "";
  return (
    <div className={styles.step}>
      <button type="button" className={styles.stepRow} onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className={`${styles.stepIcon} ${iconClass}`}>
          <Icon name={call.status === "error" && !denied ? "x" : v.icon} size={14} />
        </span>
        <span className={styles.stepLabel}>{v.label}</span>
        <span className={styles.stepTarget} title={v.target}>
          {v.target}
        </span>
        {denied && (
          <span className={styles.toolDenied} title="自动模式判定这一步有风险，没有执行">
            被自动模式拦下{call.deniedReason ? `：${call.deniedReason}` : ""}
          </span>
        )}
        {call.fallbackApplied && <span className={styles.toolDenied}>已降级</span>}
        <span className={styles.stepTime}>{running ? <span className={styles.spin} /> : stepDuration(call.durationMs)}</span>
      </button>
      {open && <div className={styles.stepDetail}>{detail(call)}</div>}
      {/* 保存 / 遗忘记忆：当场提示，可以撤销（doc/memory-design.html §4.7） */}
      {call.artifact?.memory && <MemoryNotice memory={call.artifact.memory} />}
    </div>
  );
}

export function Steps({ calls, live }: { calls: ToolCall[]; live: boolean }) {
  const running = live && calls.some((c) => c.status === "running");
  const failed = calls.some((c) => c.status === "error" && !isAutoDenied(c));
  const [open, setOpen] = useState(running);
  const touched = useRef(false);
  useEffect(() => {
    if (!touched.current) setOpen(running);
  }, [running]);

  return (
    <div className={styles.steps}>
      <button
        type="button"
        className={styles.stepsHead}
        aria-expanded={open}
        onClick={() => {
          touched.current = true;
          setOpen((o) => !o);
        }}
      >
        {running ? (
          <span className={styles.spin} />
        ) : (
          <Icon name={failed ? "alert" : "check"} size={15} style={{ color: failed ? "var(--warning)" : "var(--fg-3)" }} />
        )}
        <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {stepsTitle(calls, live)}
        </span>
        <Icon name={open ? "down" : "right"} size={14} style={{ marginLeft: "auto", color: "var(--fg-4)", flex: "none" }} />
      </button>
      {open && (
        <div className={styles.stepsBody}>
          {calls.map((c) => (
            <Step key={c.callId} call={c} live={live} />
          ))}
        </div>
      )}
    </div>
  );
}
