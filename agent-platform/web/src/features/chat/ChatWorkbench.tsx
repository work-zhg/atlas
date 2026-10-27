"use client";

import { Alert, Button, Empty, Modal, Select } from "antd";
import { useRouter } from "next/navigation";
import { useCallback, useMemo, useState } from "react";

import styles from "@atlas/chat-ui/src/chat/chat.module.css";
import { ThreadConversation, type ConversationStreamState } from "@atlas/chat-ui/src/chat/ThreadConversation";
import { useAgents } from "@/api/agents";
import { useCreateThread, useThread, useThreads } from "@/api/threads";
import { Icon } from "@/components/Icon";
import { BackfillNotice } from "@/features/memory/BackfillNotice";
import { appName, ThreadList } from "./ThreadList";
import type { ContextUsageData } from "@atlas/chat-ui/src/lib/events";

import { ContextMeter } from "./ContextMeter";
import { FilesPanel } from "./FilesPanel";

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
  const agentsQ = useAgents({ status: "enabled" });
  const createThread = useCreateThread();

  // 对话区（消息流、发送、取消、实时轨迹）在共享组件里；这里只拿流状态给文件面板联动
  const [live, setLive] = useState<{
    running: boolean;
    filesWritten: number;
    usage?: ContextUsageData;
  }>({ running: false, filesWritten: 0 });
  const onStreamState = useCallback((s: ConversationStreamState) => {
    const usage = s.state?.contextUsage;
    setLive((prev) =>
      prev.running === s.running && prev.filesWritten === s.filesWritten && prev.usage === usage
        ? prev
        : { running: s.running, filesWritten: s.filesWritten, usage },
    );
  }, []);

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

      {threadId ? (
        <ThreadConversation
          key={threadId}
          threadId={threadId}
          onStreamState={onStreamState}
          // 业务应用建的会话只读：续聊要回到那个应用，不能绕过它的流程状态（内网身份传递 §7.3）
          readOnly={!!agent?.source_app}
          readOnlyHint={
            agent?.source_app && `这个会话来自 ${appName(agent.source_app)}，请回到 ${appName(agent.source_app)} 里继续`
          }
          // 会话后补记写了什么，下次见到用户时告诉他（doc/memory-design.html §4.3）
          notice={<BackfillNotice linkToPage />}
          headerExtra={
            agent && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
              <ContextMeter threadId={threadId} live={live.usage} running={live.running} />
              <Button
                size="small"
                type="text"
                icon={<Icon name="cog" size={14} />}
                onClick={() => router.push(`/agents/${agent.agent_id}`)}
              >
                智能体配置
              </Button>
              </span>
            )
          }
        />
      ) : (
        <section className={`${styles.pane} ${styles.chat}`} aria-label="对话">
          <header className={`${styles.paneHead} ${styles.chatHead}`}>
            <span style={{ color: "var(--fg-3)", fontSize: 13 }}>未选择会话</span>
          </header>
          <div style={{ flex: 1, display: "grid", placeItems: "center", alignContent: "center" }}>
            <div style={{ maxWidth: 560, width: "100%" }}>
              <BackfillNotice linkToPage />
            </div>
            <Empty description="选择左侧会话，或新建一个">
              <Button type="primary" icon={<Icon name="plus" size={14} />} onClick={() => setNewOpen(true)}>
                新建会话
              </Button>
            </Empty>
          </div>
        </section>
      )}

      <FilesPanel threadId={threadId} running={live.running} writtenCount={live.filesWritten} />

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
