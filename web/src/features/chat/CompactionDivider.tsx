"use client";

import { Alert } from "antd";
import { useState } from "react";

import { Icon } from "@/components/Icon";
import { compactNumber } from "@/lib/format";
import type { CompactionMark } from "@/lib/run-reducer";
import styles from "./chat.module.css";

/**
 * 上下文压缩分隔条（文档 §7.5）。
 *
 * **为什么这是必需的而不是可选的**：用户一定会遇到「agent 忘了我前面说过的
 * 话」。没有可见标记时，这是个无法解释、无法复现的 bug 报告；有了标记，
 * 用户知道发生了什么，并且能自己决定要不要新建会话。
 *
 * 视觉权重刻意低于 SubAgentCard —— 它是系统行为，不是 agent 行为
 * （MASTER.md：用 --fg-3 + --line，不用色块）。
 */
export function CompactionDivider({ mark }: { mark: CompactionMark }) {
  const [open, setOpen] = useState(false);

  return (
    <div style={{ maxWidth: 820, margin: "0 auto var(--s-6)", padding: "0 var(--s-6)" }}>
      <div className={styles.compaction}>
        <Icon name="layers" size={13} />
        {mark.degraded ? (
          <span>早期 {mark.messagesSummarized} 条消息已截断（摘要生成失败）</span>
        ) : (
          <span>
            早期 {mark.messagesSummarized} 条消息已压缩为摘要（
            {compactNumber(mark.tokensBefore)} → {compactNumber(mark.tokensAfter)} tokens）
          </span>
        )}
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          style={{
            background: "none",
            border: "none",
            color: "var(--primary-text)",
            cursor: "pointer",
            fontSize: 11.5,
            padding: 0,
          }}
        >
          {open ? "收起" : "查看摘要"}
        </button>
      </div>

      {open && (
        <Alert
          type={mark.degraded ? "warning" : "info"}
          style={{ marginTop: "var(--s-3)" }}
          message={
            <span style={{ fontSize: 12.5, whiteSpace: "pre-wrap" }}>
              {mark.summary || "（无摘要内容）"}
            </span>
          }
        />
      )}

      {/* 原文没有丢 —— §7.4：模型看不到，但用户往上滚仍能看到全部。
          不说这一句，用户会以为消息被删了。 */}
      {open && !mark.degraded && (
        <p style={{ fontSize: 11, color: "var(--fg-3)", marginTop: 6 }}>
          原始消息仍完整保留，向上滚动可以看到。摘要只影响模型看到的内容。
        </p>
      )}
    </div>
  );
}
