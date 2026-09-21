"use client";

import { Alert, Button, Empty, Modal, Select, Tag } from "antd";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { useAgents } from "@/api/agents";
import { useCancelRun, useSendMessage } from "@/api/runs";
import { flattenMessages, useCreateThread, useMessages, useThread, useThreads } from "@/api/threads";
import { Icon } from "@/components/Icon";
import { avatarBackground, avatarLetter } from "@/features/agents/avatar";
import { isStreamDone } from "@/lib/run-reducer";
import styles from "./chat.module.css";
import { ApprovalModal } from "./ApprovalModal";
import { newIdempotencyKey } from "@/lib/id";
import { Composer } from "./Composer";
import { MessageStream } from "./MessageStream";
import { ThreadList } from "./ThreadList";
import { TraceInspector } from "./TraceInspector";
import { useRunStream } from "./useRunStream";

export function ChatWorkbench({ threadId }: { threadId?: string }) {
  const router = useRouter();
  const [activeRunId, setActiveRunId] = useState<string>();
  const [newOpen, setNewOpen] = useState(false);
  // 已决策的 approval：事件流里那条不会消失，靠本地集合把弹窗关掉
  const [decided, setDecided] = useState<Set<string>>(new Set());
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

  const { state, reconnecting, streamError, reset } = useRunStream({
    runId: activeRunId,
    threadId,
  });

  // ★ 刷新/直接打开链接时恢复在跑的 run。activeRunId 原本只在「发消息
  //   成功」时赋值，重新挂载后是 undefined —— 于是不订阅任何流，而助手
  //   消息要到 message.completed 才落库，长 run 期间页面上只剩用户那条
  //   提问，看着像「回答到一半全没了」。
  //   续传本身早就支持（Last-Event-ID + run_event 归档），缺的只是入口。
  const restorable = threadQ.data?.active_run_id ?? undefined;
  useEffect(() => {
    if (restorable && !activeRunId) setActiveRunId(restorable);
  }, [restorable, activeRunId]);

  // 切换会话时清空上一轮的实时状态，否则新会话会短暂显示旧会话的轨迹
  useEffect(() => {
    setActiveRunId(undefined);
    reset();
  }, [threadId, reset]);

  const running = Boolean(activeRunId) && !isStreamDone(state.status);
  const pendingApproval = state.pendingApprovals.find((a) => !decided.has(a.approvalId));

  const handleSend = useCallback(
    (text: string) => {
      if (!threadId) return;
      reset();
      sendMessage.mutate(
        {
          threadId,
          content: [{ type: "text", text }],
          // ★ 不用 crypto.randomUUID()：它只在安全上下文可用（HTTPS/localhost），
          //   用 http 访问域名时是 undefined，点发送会直接抛 TypeError ——
          //   表现是"输入框毫无反应"，请求根本没发出去。见 lib/id.ts。
          idempotencyKey: newIdempotencyKey(),
        },
        { onSuccess: (res) => setActiveRunId(res.run_id) },
      );
    },
    [threadId, sendMessage, reset],
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
                <span style={{ fontSize: 13, color: "var(--fg-2)" }}>{agent.title}</span>
                {running && (
                  <Tag color="success" style={{ marginInlineEnd: 0 }}>
                    运行中
                  </Tag>
                )}
              </>
            ) : (
              <span style={{ color: "var(--fg-3)", fontSize: 13 }}>未选择会话</span>
            )}
          </div>
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

      <ApprovalModal
        // ★ 优先用审批自带的 run：委派来的审批属于**子 run**，提交到父 run
        //   的端点会 404（services/subagent.py::_forward_approvals 转发时
        //   带上了 run_id）。
        runId={pendingApproval?.runId ?? activeRunId}
        approval={pendingApproval}
        onDecided={(id) => setDecided((prev) => new Set(prev).add(id))}
      />

      <TraceInspector state={state} />

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
