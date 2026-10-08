"use client";

import { Alert, Button, Empty, Modal, Select, Tag } from "antd";
import { useRouter } from "next/navigation";
import { useCallback, useMemo, useState } from "react";

import { useAgents } from "@/api/agents";
import { useCancelRun, useSendMessage } from "@/api/runs";
import { flattenMessages, useCreateThread, useMessages, useThread, useThreads } from "@/api/threads";
import { Icon } from "@/components/Icon";
import { avatarBackground, avatarLetter } from "@/features/agents/avatar";
import { isStreamDone } from "@/lib/run-reducer";
import styles from "./chat.module.css";
import { newIdempotencyKey } from "@/lib/id";
import { Composer } from "./Composer";
import { MessageStream } from "./MessageStream";
import { ThreadList } from "./ThreadList";
import { FilesPanel } from "./FilesPanel";
import { useThreadStream } from "./useThreadStream";

export function ChatWorkbench({ threadId }: { threadId?: string }) {
  const router = useRouter();
  const [newOpen, setNewOpen] = useState(false);
  const [pickedAgent, setPickedAgent] = useState<string>();

  const threadsQ = useThreads();
  const threads = useMemo(
    () => threadsQ.data?.pages.flatMap((p) => p.data) ?? [],
    [threadsQ.data],
  );

  const threadQ = useThread(threadId);
  const messagesQ = useMessages(threadId);
  const messages = useMemo(() => flattenMessages(messagesQ.data?.pages), [messagesQ.data]);

  const agentsQ = useAgents({ status: "enabled" });
  const createThread = useCreateThread();
  const sendMessage = useSendMessage();
  const cancelRun = useCancelRun();

  // ★ 订阅**会话**而不是 run。
  //
  //   原先这里要先知道"当前是哪个 run"才能订阅，于是刷新页面后没有入口
  //   （activeRunId 只在发消息成功时赋值），长 run 期间页面上只剩用户那条
  //   提问 —— 看着像「回答到一半全没了」。那段恢复逻辑整个消失了：订阅会话
  //   天然就是恢复。当前是哪个 run 由事件流自己说（state.activeRunId）。
  const { state, reconnecting, streamError } = useThreadStream({ threadId });
  const activeRunId = state.activeRunId;

  const running = Boolean(activeRunId) && !isStreamDone(state.status);
  // 头部状态：审批优先（用户要动手），其次是挂起（在等子智能体），最后是运行中
  const liveTag = !running
    ? null
    : state.pendingApprovals.length > 0
      ? { label: "待审批 · 已挂起", color: "warning" }
      : state.status === "suspended"
        ? { label: "等待子智能体", color: "processing" }
        : { label: "运行中", color: "success" };
  // ★ 审批不再是弹窗：它作为一张卡片出现在对话里它发生的位置（MessageStream →
  //   ApprovalCard），「是否还在等」的查库核对也随卡片走。

  const handleSend = useCallback(
    (text: string) => {
      if (!threadId) return;
      // ★ 不 reset：那会把会话流的游标一起归零，下次重连要重放一遍。
      //   上一轮的轨迹由新一轮的 run.started 清掉（reducer 的 freshTurn）。
      sendMessage.mutate(
        {
          threadId,
          content: [{ type: "text", text }],
          // ★ 不用 crypto.randomUUID()：它只在安全上下文可用（HTTPS/localhost），
          //   用 http 访问域名时是 undefined，点发送会直接抛 TypeError ——
          //   表现是"输入框毫无反应"，请求根本没发出去。见 lib/id.ts。
          idempotencyKey: newIdempotencyKey(),
        },
        // ★ 不再需要把 run_id 记下来 —— 会话流里的 run.started 会带来它。
      );
    },
    [threadId, sendMessage],
  );

  const handleNewThread = () => {
    if (!pickedAgent) return;
    createThread.mutate(
      // title 传空串 = 用后端默认值。openapi-typescript 把「有 default 的字段」
      // 一律生成为必填（对响应成立，对请求不成立），所以这里必须显式给。
      // 空标题的会话由 §8 的标题生成填充（P5）。
      { agent_id: pickedAgent, title: "" },
      {
        onSuccess: (t) => {
          setNewOpen(false);
          router.push(`/chat/${t.id}`);
        },
      },
    );
  };

  const agent = threadQ.data;

  return (
    <div className={styles.screen}>
      <ThreadList
        threads={threads}
        activeId={threadId}
        loading={threadsQ.isPending}
        hasMore={Boolean(threadsQ.hasNextPage)}
        loadingMore={threadsQ.isFetchingNextPage}
        onSelect={(id) => router.push(`/chat/${id}`)}
        onNew={() => {
          setPickedAgent(agentsQ.data?.data[0]?.id);
          setNewOpen(true);
        }}
        onLoadMore={() => void threadsQ.fetchNextPage()}
      />

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
              <span style={{ color: "var(--fg-3)", fontSize: 13 }}>未选择会话</span>
            )}
          </div>
          {agent && (
            <Button
              size="small"
              type="text"
              style={{ marginLeft: "auto" }}
              icon={<Icon name="cog" size={14} />}
              onClick={() => router.push(`/agents/${agent.agent_id}`)}
            >
              智能体配置
            </Button>
          )}
        </header>

        {threadId ? (
          <>
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
              <div style={{ maxWidth: 820, margin: "0 auto var(--s-3)", padding: "0 var(--s-6)" }}>
                <Alert
                  type="error"
                  showIcon
                  message="发送失败"
                  description={sendMessage.error.message}
                  closable
                />
              </div>
            )}

            <Composer
              disabled={false}
              running={running}
              onSend={handleSend}
              onCancel={() => activeRunId && cancelRun.mutate(activeRunId)}
            />
          </>
        ) : (
          <div style={{ flex: 1, display: "grid", placeItems: "center" }}>
            <Empty description="选择左侧会话，或新建一个">
              <Button type="primary" icon={<Icon name="plus" size={14} />} onClick={() => setNewOpen(true)}>
                新建会话
              </Button>
            </Empty>
          </div>
        )}
      </section>

      <FilesPanel threadId={threadId} running={running} writtenCount={state.files.length} />

      <Modal
        open={newOpen}
        title="新建会话"
        okText="创建"
        cancelText="取消"
        confirmLoading={createThread.isPending}
        onOk={handleNewThread}
        onCancel={() => setNewOpen(false)}
      >
        <p style={{ color: "var(--fg-2)", fontSize: 13 }}>选择这个会话使用的智能体：</p>
        <Select
          style={{ width: "100%" }}
          value={pickedAgent}
          onChange={setPickedAgent}
          loading={agentsQ.isPending}
          placeholder="选择智能体"
          options={(agentsQ.data?.data ?? []).map((a) => ({
            value: a.id,
            label: `${a.name} · ${a.model}`,
          }))}
        />
        {/* ★ 只有已启用的智能体能开会话。一个都没有时必须说清楚是"草稿还没启用"，
            而不是丢一个空下拉 —— 那会让人以为是加载失败。 */}
        {!agentsQ.isPending && (agentsQ.data?.data.length ?? 0) === 0 && (
          <Alert
            style={{ marginTop: 12 }}
            type="info"
            showIcon
            message="还没有已启用的智能体"
            description="新建的智能体默认是草稿态，需要在智能体页面点「启用」后才能开会话。"
            action={
              <Button
                size="small"
                type="link"
                onClick={() => {
                  setNewOpen(false);
                  router.push("/agents");
                }}
              >
                去启用
              </Button>
            }
          />
        )}
      </Modal>
    </div>
  );
}
