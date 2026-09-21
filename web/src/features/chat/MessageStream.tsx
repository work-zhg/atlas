"use client";

import { Alert, Button, Skeleton, Tag } from "antd";
import { useEffect, useRef } from "react";

import type { Message } from "@/api/types";
import { Icon } from "@/components/Icon";
import { Markdown } from "@/components/Markdown";
import { relativeTime } from "@/lib/format";
import type { RunState } from "@/lib/run-reducer";
import { avatarBackground, avatarLetter } from "@/features/agents/avatar";
import { CompactionDivider } from "./CompactionDivider";
import styles from "./chat.module.css";
import { SubAgentCard } from "./SubAgentCard";
import { TodoCard } from "./TodoCard";
import { ToolCallRow } from "./ToolCallRow";

interface Props {
  messages: Message[];
  loading: boolean;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  /** 当前 run 的实时状态；无进行中的 run 时为 undefined */
  live?: RunState;
  /** 实时块对应的 run —— 用于判断持久化消息是否已覆盖它，避免重复渲染 */
  liveRunId?: string;
  agentName: string;
  agentAvatarKey: string;
  reconnecting: boolean;
  streamError: Error | null;
}

function textOf(content: Message["content"]): string {
  return blocksOf(content).join("");
}

/**
 * 正文分段。一个 run 里模型可能说好几轮 —— 调工具前一段、拿到结果后一段，
 * 后端按轮切成多个 text block（server 的 domain/events.py::Answer）。
 * 这里不再 join：最后一段是结论，前面的是过程，要分开渲染。
 */
function blocksOf(content: Message["content"]): string[] {
  return content
    .filter((b) => b.type === "text" && typeof b.text === "string")
    .map((b) => b.text as string)
    .filter((t) => t.length > 0);
}

/** 助手正文：末段是结论，之前的都是过程性发言，弱化显示。 */
function AssistantText({ blocks }: { blocks: string[] }) {
  if (blocks.length === 0) return null;
  // ★ ?? ""：noUncheckedIndexedAccess 下下标访问是 string | undefined
  const answer = blocks[blocks.length - 1] ?? "";
  const aside = blocks.slice(0, -1);
  return (
    <>
      {aside.map((t, i) => (
        // eslint-disable-next-line react/no-array-index-key -- 段序即身份，段不会重排
        <Markdown key={i} className={`${styles.prose} ${styles.proseAside}`}>{t}</Markdown>
      ))}
      <Markdown className={styles.prose}>{answer}</Markdown>
    </>
  );
}

export function MessageStream({
  messages,
  loading,
  hasMore,
  loadingMore,
  onLoadMore,
  live,
  liveRunId,
  agentName,
  agentAvatarKey,
  reconnecting,
  streamError,
}: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const liveText = live?.text ?? "";

  // 新内容到达时贴底。仅在用户已经接近底部时才自动滚 ——
  // 否则会把正在往回翻历史的用户强行拽下来。
  const bodyRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    if (nearBottom) bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages.length, liveText]);

  // 助手消息已落库后就不再渲染实时块，避免同一段话出现两遍
  const persisted = liveRunId ? messages.some((m) => m.run_id === liveRunId && m.role === "assistant") : false;
  const showLive = Boolean(live) && !persisted && live!.status !== "idle";

  return (
    <div className={styles.paneBody} ref={bodyRef}>
      {loading && (
        <div style={{ maxWidth: 820, margin: "0 auto", padding: "var(--s-6)" }}>
          <Skeleton active paragraph={{ rows: 4 }} />
        </div>
      )}

      {hasMore && (
        <div style={{ display: "flex", justifyContent: "center", padding: "var(--s-4)" }}>
          <Button size="small" loading={loadingMore} onClick={onLoadMore}>
            加载更早的消息
          </Button>
        </div>
      )}

      {messages.map((m) =>
        m.role === "user" ? (
          <div key={m.id} className={`${styles.msg} ${styles.msgUser}`}>
            <div className={styles.bubble}>{textOf(m.content)}</div>
          </div>
        ) : (
          <div key={m.id} className={styles.msg}>
            <div className={styles.msgHead}>
              <span
                className={styles.agentAvatar}
                style={{ background: avatarBackground(agentAvatarKey) }}
                aria-hidden="true"
              >
                {avatarLetter(agentName)}
              </span>
              <span className={styles.msgName}>{agentName}</span>
              <span className={styles.msgMeta}>{relativeTime(m.created_at)}</span>
            </div>
            <AssistantText blocks={blocksOf(m.content)} />
          </div>
        ),
      )}

      {showLive && live && (
        <div className={styles.msg}>
          <div className={styles.msgHead}>
            <span
              className={styles.agentAvatar}
              style={{ background: avatarBackground(agentAvatarKey) }}
              aria-hidden="true"
            >
              {avatarLetter(agentName)}
            </span>
            <span className={styles.msgName}>{agentName}</span>
            {live.meta && (
              <Tag style={{ marginInlineEnd: 0 }} className={styles.toolName}>
                {live.meta.model}
              </Tag>
            )}
            {reconnecting && (
              <Tag color="warning" style={{ marginInlineEnd: 0 }}>
                重新连接中…
              </Tag>
            )}
          </div>

          {/* §13.2：请求了但未接入的工具必须报出来，不能静默忽略 */}
          {live.meta?.unsupported_tools && live.meta.unsupported_tools.length > 0 && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: "var(--s-4)" }}
              message={`以下工具本期未接入，本轮不会被调用：${live.meta.unsupported_tools.join("、")}`}
            />
          )}

          {/* §7.5：压缩必须对用户可见，否则就是「它忘了我说的话」这种
              无法解释的 bug 报告 */}
          {live.compactions.map((c) => (
            <CompactionDivider key={c.seq} mark={c} />
          ))}

          {live.todos.length > 0 && <TodoCard todos={live.todos} />}

          {live.subagents.map((s) => (
            <SubAgentCard key={s.runId} run={s} />
          ))}

          {live.toolCalls.length > 0 && (
            <div style={{ marginBottom: "var(--s-4)" }}>
              {live.toolCalls.map((c) => (
                <ToolCallRow key={c.callId} call={c} />
              ))}
            </div>
          )}

          {live.text && <AssistantText blocks={live.blocks.filter((b) => b.length > 0)} />}

          {live.status === "running" && <span className={styles.cursor} aria-hidden="true" />}

          {live.status === "failed" && live.error && (
            <Alert
              type="error"
              showIcon
              style={{ marginTop: "var(--s-3)" }}
              message={`运行失败（${live.error.kind}）`}
              description={live.error.message}
            />
          )}

          {live.status === "cancelled" && (
            <Alert type="info" showIcon style={{ marginTop: "var(--s-3)" }} message="已取消" />
          )}
        </div>
      )}

      {streamError && (
        <div className={styles.msg}>
          <Alert
            type="error"
            showIcon
            icon={<Icon name="warn" size={16} />}
            message="事件流连接失败"
            description={streamError.message}
          />
        </div>
      )}

      <div ref={bottomRef} style={{ height: 1 }} />
    </div>
  );
}
