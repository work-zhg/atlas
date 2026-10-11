"use client";

/**
 * 一条会话的完整对话区：头部（智能体、模型、状态）+ 消息流（含实时轨迹、审批、子智能体）+ 输入框。
 *
 * ★ Atlas 对话页与 AI TeamFlow 的人机协同用的都是它 —— 对话的组装逻辑只有这一份。
 *   接口地址与身份头由外层 <AtlasClientProvider> 注入；嵌入方差异只经 props 表达
 *   （只读、头部右侧的按钮、流状态回调），不在这里写任何嵌入方的业务。
 */
import { Alert, Tag } from "antd";
import { useCallback, useEffect, useMemo, type ReactNode } from "react";

import { useCancelRun, useSendMessage } from "../api/runs";
import { flattenMessages, useMessages, useThread } from "../api/threads";
import { avatarBackground, avatarLetter } from "../lib/avatar";
import { newIdempotencyKey } from "../lib/id";
import { isStreamDone, type RunState } from "../lib/run-reducer";
import styles from "./chat.module.css";
import { Composer } from "./Composer";
import { MessageStream } from "./MessageStream";
import { useThreadStream } from "./useThreadStream";

export interface ConversationStreamState {
  running: boolean;
  /** 本轮 file.written 的条数（文件面板据此刷新） */
  filesWritten: number;
  state: RunState;
}

export interface ThreadConversationProps {
  threadId: string;
  /** 只读：不显示输入框（如：没有协同权限、节点不在协同中） */
  readOnly?: boolean;
  /** 只读时在输入框位置显示的说明 */
  readOnlyHint?: ReactNode;
  /** 头部右侧的附加操作（如 Atlas 的「智能体配置」按钮） */
  headerExtra?: ReactNode;
  /** 流状态变化回调（运行中、写文件数），供外层面板联动 */
  onStreamState?: (s: ConversationStreamState) => void;
}

export function ThreadConversation({
  threadId,
  readOnly = false,
  readOnlyHint,
  headerExtra,
  onStreamState,
}: ThreadConversationProps) {
  const threadQ = useThread(threadId);
  const messagesQ = useMessages(threadId);
  const messages = useMemo(() => flattenMessages(messagesQ.data?.pages), [messagesQ.data]);
  const sendMessage = useSendMessage();
  const cancelRun = useCancelRun();

  // ★ 订阅**会话**而不是 run：刷新页面后天然恢复；当前是哪个 run 由事件流自己说（state.activeRunId）。
  const { state, reconnecting, streamError } = useThreadStream({ threadId });
  const activeRunId = state.activeRunId;
  const running = Boolean(activeRunId) && !isStreamDone(state.status);

  useEffect(() => {
    onStreamState?.({ running, filesWritten: state.files.length, state });
  }, [running, state, onStreamState]);

  // 头部状态：审批优先（用户要动手），其次是挂起（在等子智能体），最后是运行中
  const liveTag = !running
    ? null
    : state.pendingApprovals.length > 0
      ? { label: "待审批 · 已挂起", color: "warning" }
      : state.status === "suspended"
        ? { label: "等待子智能体", color: "processing" }
        : { label: "运行中", color: "success" };

  const handleSend = useCallback(
    (text: string) => {
      // ★ 不 reset 流：那会把会话流的游标一起归零。上一轮的轨迹由新一轮的 run.started 清掉。
      // ★ 幂等键不用 crypto.randomUUID()：它只在安全上下文可用（见 lib/id.ts）。
      sendMessage.mutate({
        threadId,
        content: [{ type: "text", text }],
        idempotencyKey: newIdempotencyKey(),
      });
    },
    [threadId, sendMessage],
  );

  const agent = threadQ.data;

  return (
    <section className={`${styles.pane} ${styles.chat}`} aria-label="对话">
      <header className={`${styles.paneHead} ${styles.chatHead}`}>
        <div className={styles.chatHeadLeft}>
          {agent ? (
            <>
              <span className={styles.agentChip}>
                <span
                  className={styles.agentAvatar}
                  style={{ background: avatarBackground(agent.agent_avatar_key) }}
                  aria-hidden="true"
                >
                  {avatarLetter(agent.agent_name)}
                </span>
                <span style={{ fontSize: 12.5, fontWeight: 550 }}>{agent.agent_name}</span>
              </span>
              {state.meta?.model && (
                <Tag className={styles.monoTag} style={{ marginInlineEnd: 0 }}>
                  {state.meta.model}
                </Tag>
              )}
              <span className={styles.chatTitle} title={agent.title}>
                {agent.title}
              </span>
              {liveTag && (
                <Tag color={liveTag.color} style={{ marginInlineEnd: 0 }}>
                  {liveTag.label}
                </Tag>
              )}
            </>
          ) : (
            <span style={{ color: "var(--fg-3)", fontSize: 13 }}>加载会话…</span>
          )}
        </div>
        {headerExtra && <div style={{ marginLeft: "auto" }}>{headerExtra}</div>}
      </header>

      <MessageStream
        messages={messages}
        loading={messagesQ.isPending}
        hasMore={Boolean(messagesQ.hasNextPage)}
        loadingMore={messagesQ.isFetchingNextPage}
        onLoadMore={() => void messagesQ.fetchNextPage()}
        live={activeRunId ? state : undefined}
        liveRunId={activeRunId}
        agentName={agent?.agent_name ?? "助手"}
        agentAvatarKey={agent?.agent_avatar_key ?? "general"}
        reconnecting={reconnecting}
        streamError={streamError}
      />

      {sendMessage.isError && (
        <div style={{ maxWidth: 820, margin: "0 auto var(--s-3)", padding: "0 var(--s-6)", width: "100%" }}>
          <Alert type="error" showIcon message="发送失败" description={sendMessage.error.message} closable />
        </div>
      )}

      {readOnly ? (
        readOnlyHint && (
          <div style={{ padding: "var(--s-3) var(--s-6)", color: "var(--fg-3)", fontSize: 12.5, textAlign: "center" }}>
            {readOnlyHint}
          </div>
        )
      ) : (
        <Composer
          disabled={false}
          running={running}
          onSend={handleSend}
          onCancel={() => activeRunId && cancelRun.mutate(activeRunId)}
        />
      )}
    </section>
  );
}
