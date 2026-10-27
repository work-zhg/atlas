"use client";

/**
 * 一条会话的完整对话区：头部（智能体、模型、状态）+ 消息流（含实时轨迹、审批、子智能体）+ 输入框。
 *
 * ★ Atlas 对话页与 AI TeamFlow 的人机协同用的都是它 —— 对话的组装逻辑只有这一份。
 *   接口地址与身份头由外层 <AtlasClientProvider> 注入；嵌入方差异只经 props 表达
 *   （只读、头部右侧的按钮、流状态回调），不在这里写任何嵌入方的业务。
 */
import { useQueryClient } from "@tanstack/react-query";
import { Alert, Select, Tag } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { qk } from "../api/keys";
import { useCancelRun, useSendMessage } from "../api/runs";
import { flattenMessages, useMessages, useThread } from "../api/threads";
import { avatarBackground, avatarLetter } from "../lib/avatar";
import { APPROVAL_MODE_LABEL, type ApprovalMode } from "../lib/events";
import { newIdempotencyKey } from "../lib/id";
import { isStreamDone, type RunState } from "../lib/run-reducer";
import styles from "./chat.module.css";
import { Composer } from "./Composer";
import { MessageStream } from "./MessageStream";
import { useThreadStream } from "./useThreadStream";

/** 选择器选项的悬停说明 */
const MODE_HINT: Record<ApprovalMode, string> = {
  plan: "只读调研，提交计划经你批准后再执行",
  manual: "写文件、执行命令、调用外部工具、委派前都先问你",
  auto: "工作区内读写和委派直接执行；执行命令、删除、调用外部工具先问你",
};

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
  /** 输入框上方的提示（如 Atlas 的「上次会话补记了 N 条记忆」）。没有要提示的就渲染成 null */
  notice?: ReactNode;
  /** 流状态变化回调（运行中、写文件数），供外层面板联动 */
  onStreamState?: (s: ConversationStreamState) => void;
}

export function ThreadConversation({
  threadId,
  readOnly = false,
  readOnlyHint,
  headerExtra,
  notice,
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

  // ── 审批模式（doc/native-approval-mode-design.html §11）。acp 会话没有 approval，不显示选择器
  const approval = threadQ.data?.approval;
  // 用户在选择器里选了、还没发出去的模式；null = 沿用会话的模式
  const [pickedMode, setPickedMode] = useState<ApprovalMode | null>(null);
  // 会话的模式变了（发消息时带上了选择、或计划获批换成了执行模式）→ 本地选择作废，以会话为准
  useEffect(() => setPickedMode(null), [threadId, approval?.mode]);
  // 计划获批时 server 改了会话的模式：一轮结束后刷新会话，选择器跟着变
  const qc = useQueryClient();
  const wasRunning = useRef(running);
  useEffect(() => {
    if (wasRunning.current && !running) {
      void qc.invalidateQueries({ queryKey: qk.threads.detail(threadId) });
    }
    wasRunning.current = running;
  }, [running, threadId, qc]);

  // 头部状态：审批优先（用户要动手），其次是挂起（在等子智能体），最后是运行中
  const liveTag = !running
    ? null
    : state.pendingApprovals.some((a) => a.kind === "question")
      ? { label: "待回答 · 已挂起", color: "warning" }
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
        // 没动过选择器就不传：会话沿用记住的模式（没有就跟着智能体的默认走）
        approvalMode: pickedMode ?? undefined,
      });
    },
    [threadId, sendMessage, pickedMode],
  );

  // P1 运行中不可切换：这一轮的模式在开始时就定了，改了也只作用于下一轮，置灰免得误会
  const modeSelector = approval && (
    <Select
      size="small"
      variant="borderless"
      aria-label="审批模式"
      popupMatchSelectWidth={false}
      disabled={running}
      value={pickedMode ?? (approval.mode as ApprovalMode)}
      onChange={(v: ApprovalMode) => setPickedMode(v)}
      options={(approval.allowed_modes as ApprovalMode[]).map((m) => ({
        value: m,
        label: `${APPROVAL_MODE_LABEL[m]}模式`,
        title: MODE_HINT[m],
      }))}
    />
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

      {notice && (
        <div style={{ maxWidth: 820, margin: "0 auto", padding: "0 var(--s-6)", width: "100%" }}>{notice}</div>
      )}

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
          toolbar={modeSelector}
        />
      )}
    </section>
  );
}
