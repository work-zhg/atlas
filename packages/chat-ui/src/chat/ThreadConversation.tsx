"use client";

/**
 * 一条会话的完整对话区：头部（标题、智能体、状态）+ 消息流（含实时轨迹、审批、子智能体）+ 输入框。
 *
 * ★ Atlas 对话页与 AI TeamFlow 的人机协同用的都是它 —— 对话的组装逻辑只有这一份。
 *   接口地址与身份头由外层 <AtlasClientProvider> 注入；嵌入方差异只经 props 表达
 *   （只读、头部右侧的按钮、流状态回调），不在这里写任何嵌入方的业务。
 */
import { useQueryClient } from "@tanstack/react-query";
import { Alert, Tag } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { qk } from "../api/keys";
import { useCancelRun, useSendMessage } from "../api/runs";
import { flattenMessages, useMessages, useThread } from "../api/threads";
import { avatarBackground, avatarLetter } from "../lib/avatar";
import type { ApprovalMode } from "../lib/events";
import { newIdempotencyKey } from "../lib/id";
import { isStreamDone, type RunState } from "../lib/run-reducer";
import styles from "./chat.module.css";
import { Composer } from "./Composer";
import { MessageStream } from "./MessageStream";
import { ModePicker } from "./ModePicker";
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
  /** 输入框上方的提示（如 Atlas 的「上次会话补记了 N 条记忆」）。没有要提示的就渲染成 null */
  notice?: ReactNode;
  /** 流状态变化回调（运行中、写文件数），供外层面板联动 */
  onStreamState?: (s: ConversationStreamState) => void;
  /** 点头部的智能体：Atlas 跳到智能体页。不给 = 不可点 */
  onAgentClick?: (agentId: string) => void;
  /**
   * 上传一个文件到这次对话的工作区，返回它在工作区里的路径。给了才显示「添加文件」：
   * 发送前先上传，消息里附上路径，智能体据此去读。
   */
  uploadFile?: (file: File) => Promise<string>;
  /** 回答下的「有帮助 / 没帮助」（嵌入方没有反馈接口时不开） */
  feedback?: boolean;
  /** 最后一条回答下的「重新生成」：用最后一个问题再问一次 */
  regenerate?: boolean;
  /** 不要头部（嵌在别的面板里，如智能体编辑页的调试预览） */
  hideHeader?: boolean;
  /** 发消息之前先做的事（调试预览：先把还没存下的草稿存掉，这一轮才用得上最新的修改） */
  beforeSend?: () => Promise<void>;
  /** 输入框的提示文字 */
  placeholder?: string;
}

/** 附件路径并进消息正文：智能体按路径去工作区里读 */
function withAttachments(text: string, paths: string[]): string {
  if (paths.length === 0) return text;
  return `${text}\n\n附件：\n${paths.map((p) => `- ${p}`).join("\n")}`;
}

export function ThreadConversation({
  threadId,
  readOnly = false,
  readOnlyHint,
  headerExtra,
  notice,
  onStreamState,
  onAgentClick,
  uploadFile,
  feedback = false,
  regenerate = false,
  hideHeader = false,
  beforeSend,
  placeholder = "继续对话…",
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

  const [uploadError, setUploadError] = useState<string | null>(null);
  const handleSend = useCallback(
    async (text: string, files: File[] = []) => {
      await beforeSend?.();
      // 先把文件放进工作区，再发消息：智能体收到消息时文件已经在了
      setUploadError(null);
      let paths: string[] = [];
      try {
        paths = uploadFile ? await Promise.all(files.map((f) => uploadFile(f))) : [];
      } catch (e) {
        setUploadError((e as Error).message);
        throw e;
      }
      // ★ 不 reset 流：那会把会话流的游标一起归零。上一轮的轨迹由新一轮的 run.started 清掉。
      // ★ 幂等键不用 crypto.randomUUID()：它只在安全上下文可用（见 lib/id.ts）。
      await sendMessage.mutateAsync({
        threadId,
        content: [{ type: "text", text: withAttachments(text, paths) }],
        idempotencyKey: newIdempotencyKey(),
        // 没动过选择器就不传：会话沿用记住的模式（没有就跟着智能体的默认走）
        approvalMode: pickedMode ?? undefined,
      });
    },
    [threadId, sendMessage, pickedMode, uploadFile, beforeSend],
  );

  // 重新生成：把最后一个问题原样再发一次（新的一轮）
  const lastQuestion = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i]!;
      if (m.role !== "user") continue;
      const text = m.content
        .filter((b) => b.type === "text" && typeof b.text === "string")
        .map((b) => b.text as string)
        .join("");
      if (text) return text;
    }
    return undefined;
  }, [messages]);
  const onRegenerate =
    regenerate && lastQuestion && !running && !readOnly
      ? () => void handleSend(lastQuestion).catch(() => undefined)
      : undefined;

  // 运行中不可切换：这一轮的模式在开始时就定了，改了也只作用于下一轮，置灰免得误会
  const modeSelector = approval && (
    <ModePicker
      value={pickedMode ?? (approval.mode as ApprovalMode)}
      allowed={approval.allowed_modes as ApprovalMode[]}
      onChange={setPickedMode}
      disabled={running}
    />
  );

  const agent = threadQ.data;

  return (
    <section className={`${styles.pane} ${styles.chat}`} aria-label="对话">
      {!hideHeader && (
      <header className={`${styles.paneHead} ${styles.chatHead}`}>
        <div className={styles.chatHeadLeft}>
          {agent ? (
            <>
              <span className={styles.chatTitle} title={agent.title}>
                {agent.title || "新对话"}
              </span>
              {(() => {
                const chip = (
                  <>
                    <span
                      className={styles.agentAvatar}
                      style={{ background: avatarBackground(agent.agent_avatar_key), width: 16, height: 16, fontSize: 9, borderRadius: 5 }}
                      aria-hidden="true"
                    >
                      {avatarLetter(agent.agent_name)}
                    </span>
                    <span style={{ fontSize: 12.5, fontWeight: 550 }}>{agent.agent_name}</span>
                  </>
                );
                return onAgentClick ? (
                  <button type="button" className={styles.agentChip} onClick={() => onAgentClick(agent.agent_id)} title="查看智能体">
                    {chip}
                  </button>
                ) : (
                  <span className={styles.agentChip}>{chip}</span>
                );
              })()}
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
      )}

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
        threadId={threadId}
        feedback={feedback}
        onRegenerate={onRegenerate}
      />

      {notice && (
        <div style={{ maxWidth: 820, margin: "0 auto", padding: "0 var(--s-6)", width: "100%" }}>{notice}</div>
      )}

      {uploadError && (
        <div style={{ maxWidth: 820, margin: "0 auto var(--s-3)", padding: "0 var(--s-6)", width: "100%" }}>
          <Alert type="error" showIcon message="文件上传失败，消息没有发出" description={uploadError} closable onClose={() => setUploadError(null)} />
        </div>
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
          running={running}
          onSend={handleSend}
          onCancel={() => activeRunId && cancelRun.mutate(activeRunId)}
          toolbar={modeSelector}
          placeholder={placeholder}
          acceptFiles={Boolean(uploadFile)}
          foot={hideHeader ? undefined : "内容由 AI 生成，请核实重要信息"}
        />
      )}
    </section>
  );
}
