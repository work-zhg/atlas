"use client";

/**
 * 会话头部的上下文用量条（doc/context-management-design.html §16）。
 *
 * 数据来源：运行中用流里最新的 context.usage；打开老会话时取 GET /v1/threads/{id}/context
 * （最近一次归档的 context.usage）。点开看构成（系统提示词 / 工具定义 / 摘要 / 对话 / 工具结果 /
 * 附件各占多少），并可以手动压缩（可附「重点保留什么」）。
 */
import type { ContextUsageData } from "@atlas/chat-ui/src/lib/events";
import {
  Alert,
  App,
  Button,
  Descriptions,
  Input,
  Modal,
  Progress,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useState } from "react";

import { useCompactThread, useThreadContext } from "@/api/context";

const LABELS: Record<string, string> = {
  system: "系统提示词与指令",
  tools: "工具定义",
  summary: "早期对话摘要",
  conversation: "对话",
  tool_results: "工具结果",
  attachments: "本轮附件（记忆、提醒等）",
};

function color(pct: number, usage: ContextUsageData): string {
  const eff = usage.effective_window || usage.context_window;
  const t = usage.thresholds;
  if (t?.compact && usage.tokens >= t.compact) return "#cf1322";
  if (t?.clear && usage.tokens >= t.clear) return "#d48806";
  return pct >= 80 ? "#d48806" : eff ? "#1677ff" : "#1677ff";
}

function fmt(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k` : String(n);
}

export function ContextMeter({
  threadId,
  live,
  running,
}: {
  threadId: string;
  live?: ContextUsageData;
  running: boolean;
}) {
  const { message } = App.useApp();
  const [open, setOpen] = useState(false);
  const [focus, setFocus] = useState("");
  const status = useThreadContext(threadId, !live);
  const compact = useCompactThread(threadId);
  const usage = live ?? status.data?.usage ?? undefined;
  if (!usage) return null;

  const eff = usage.effective_window || usage.context_window || 1;
  const pct = Math.min(100, Math.round((usage.tokens * 100) / eff));
  const comp = usage.composition;
  const total = comp ? Object.values(comp).reduce((a, b) => a + b, 0) : 0;

  async function doCompact() {
    try {
      const r = await compact.mutateAsync(focus);
      message.success(`已压缩 ${r.messages_summarized} 条消息，下一轮生效`);
      setOpen(false);
      setFocus("");
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <>
      <Tooltip
        title={`上下文已用 ${fmt(usage.tokens)} / ${fmt(eff)} tokens（${usage.basis === "usage" ? "模型实际用量" : usage.basis === "acp" ? "CLI 上报" : "估算"}）`}
      >
        <button
          type="button"
          onClick={() => setOpen(true)}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            background: "none",
            border: "none",
            cursor: "pointer",
            padding: "0 4px",
          }}
        >
          <Progress
            percent={pct}
            size={[80, 6]}
            showInfo={false}
            strokeColor={color(pct, usage)}
          />
          <span style={{ fontSize: 12, color: "var(--fg-3)" }}>{pct}%</span>
        </button>
      </Tooltip>
      <Modal
        title="上下文"
        open={open}
        onCancel={() => setOpen(false)}
        footer={null}
        width={560}
        destroyOnHidden
      >
        <Descriptions
          size="small"
          column={1}
          bordered
          style={{ marginBottom: 12 }}
        >
          <Descriptions.Item label="已用">
            {fmt(usage.tokens)} / {fmt(eff)} tokens（{pct}%）
            {usage.basis !== "usage" && (
              <Tag style={{ marginLeft: 8 }}>
                {usage.basis === "acp" ? "CLI 上报" : "估算"}
              </Tag>
            )}
          </Descriptions.Item>
          {usage.thresholds && (
            <Descriptions.Item label="档位">
              {usage.thresholds.clear
                ? `清理旧工具结果 ${fmt(usage.thresholds.clear)} · `
                : ""}
              自动压缩 {fmt(usage.thresholds.compact)}
              {usage.thresholds.warn
                ? ` · 提醒 ${fmt(usage.thresholds.warn)}`
                : ""}
            </Descriptions.Item>
          )}
          {status.data?.summary.present && (
            <Descriptions.Item label="摘要">
              已压缩 {status.data.summary.compact_count} 次，当前摘要{" "}
              {status.data.summary.chars} 字
            </Descriptions.Item>
          )}
        </Descriptions>
        {comp && total > 0 && (
          <div style={{ marginBottom: 12 }}>
            <Typography.Text strong style={{ fontSize: 13 }}>
              构成（最近一次模型调用，估算）
            </Typography.Text>
            {Object.entries(comp)
              .filter(([, v]) => v > 0)
              .sort((a, b) => b[1] - a[1])
              .map(([k, v]) => (
                <div
                  key={k}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    fontSize: 12.5,
                    marginTop: 6,
                  }}
                >
                  <span style={{ width: 150 }}>{LABELS[k] ?? k}</span>
                  <Progress
                    percent={Math.round((v * 100) / total)}
                    size="small"
                    style={{ flex: 1, margin: 0 }}
                  />
                  <span
                    style={{
                      width: 52,
                      textAlign: "right",
                      color: "var(--fg-3)",
                    }}
                  >
                    {fmt(v)}
                  </span>
                </div>
              ))}
            {usage.attachments && usage.attachments.length > 0 && (
              <div style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 6 }}>
                本轮附件：{usage.attachments.join("、")}
              </div>
            )}
          </div>
        )}
        {(usage.compaction_disabled || status.data?.compaction_disabled) && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="自动压缩连续失败，已暂停；可以手动压缩或新开会话"
          />
        )}
        {usage.basis === "acp" ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            这个智能体由 CLI 自行管理上下文，平台只显示用量。
          </Typography.Text>
        ) : (
          <>
            <Input.TextArea
              rows={2}
              placeholder="（可选）希望摘要重点保留什么，例如：保留所有接口字段的约定"
              value={focus}
              onChange={(e) => setFocus(e.target.value)}
              maxLength={2000}
            />
            <Button
              type="primary"
              style={{ marginTop: 8 }}
              loading={compact.isPending}
              disabled={running}
              onClick={doCompact}
            >
              立即压缩
            </Button>
            <Typography.Text
              type="secondary"
              style={{ fontSize: 12, marginLeft: 8 }}
            >
              {running
                ? "本轮结束后才能压缩"
                : "把到目前为止的对话压成摘要，原始消息仍完整保留"}
            </Typography.Text>
          </>
        )}
      </Modal>
    </>
  );
}
