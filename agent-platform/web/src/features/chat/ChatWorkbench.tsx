"use client";

/**
 * 对话页：对话区占满（会话列表在左侧菜单的「最近对话」里，文件是按需打开的抽屉）。
 *
 * 对话本身（消息流、发送、取消、实时轨迹）在共享组件 ThreadConversation 里；这里只放
 * Atlas 自己的东西：头部的文件 / 上下文 / 更多、发送前上传文件、回答反馈。
 */
import { App as AntdApp, Button, Dropdown, Input, Modal } from "antd";
import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";

import { uploadThreadFile, useDeleteThread, useRenameThread, useThread } from "@/api/threads";
import { Icon } from "@/components/Icon";
import { BackfillNotice } from "@/features/memory/BackfillNotice";
import { DirChip } from "@/features/workspace/DirChip";
import { ThreadConversation, type ConversationStreamState } from "@atlas/chat-ui/src/chat/ThreadConversation";
import type { ContextUsageData } from "@atlas/chat-ui/src/lib/events";

import { ContextMeter } from "./ContextMeter";
import { FilesDrawer, useFileCount } from "./FilesPanel";
import { appName } from "./threadState";

export function ChatWorkbench({ threadId }: { threadId: string }) {
  const router = useRouter();
  const { message, modal } = AntdApp.useApp();
  const threadQ = useThread(threadId);
  const rename = useRenameThread(threadId);
  const del = useDeleteThread();
  const [filesOpen, setFilesOpen] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);

  // 流状态给文件抽屉、上下文用量联动
  const [live, setLive] = useState<{ running: boolean; filesWritten: number; usage?: ContextUsageData }>({
    running: false,
    filesWritten: 0,
  });
  const onStreamState = useCallback((s: ConversationStreamState) => {
    const usage = s.state?.contextUsage;
    setLive((prev) =>
      prev.running === s.running && prev.filesWritten === s.filesWritten && prev.usage === usage
        ? prev
        : { running: s.running, filesWritten: s.filesWritten, usage },
    );
  }, []);
  const fileCount = useFileCount(threadId, live.running);

  const thread = threadQ.data;
  const upload = useCallback((file: File) => uploadThreadFile(threadId, file), [threadId]);

  const remove = () =>
    modal.confirm({
      title: "删除这个对话？",
      content: "对话记录删除后不能恢复。工作目录里的文件不删，还在工作空间里。",
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: () =>
        del.mutateAsync(threadId).then(
          () => router.push("/"),
          (e: Error) => {
            message.error(`没有删除成功：${e.message}`);
          },
        ),
    });

  return (
    <>
      <ThreadConversation
        key={threadId}
        threadId={threadId}
        onStreamState={onStreamState}
        onAgentClick={(id) => router.push(`/agents/${id}`)}
        uploadFile={upload}
        feedback
        regenerate
        modelPicker
        // 业务应用建的会话只读：续聊要回到那个应用，不能绕过它的流程状态（内网身份传递 §7.3）
        readOnly={!!thread?.source_app}
        readOnlyHint={
          thread?.source_app &&
          `这个对话来自 ${appName(thread.source_app)}，请回到 ${appName(thread.source_app)} 里继续`
        }
        // 会话后补记写了什么，下次见到用户时告诉他（doc/memory-design.html §4.3）
        notice={<BackfillNotice linkToPage />}
        headerExtra={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            {thread?.workspace_dir && <DirChip dir={thread.workspace_dir} />}
            <ContextMeter threadId={threadId} live={live.usage} running={live.running} minPercent={60} />
            <Button size="small" type="text" icon={<Icon name="folder" size={15} />} onClick={() => setFilesOpen(true)}>
              文件{fileCount > 0 && <span style={{ color: "var(--fg-3)" }}> {fileCount}</span>}
            </Button>
            <Dropdown
              trigger={["click"]}
              placement="bottomRight"
              menu={{
                items: [
                  { key: "rename", icon: <Icon name="pencil" size={14} />, label: "重命名" },
                  { type: "divider" },
                  { key: "delete", icon: <Icon name="trash" size={14} />, label: "删除", danger: true },
                ],
                onClick: ({ key }) => {
                  if (key === "rename") setRenaming(thread?.title ?? "");
                  if (key === "delete") remove();
                },
              }}
            >
              <Button size="small" type="text" icon={<Icon name="more" size={16} />} aria-label="更多" />
            </Dropdown>
          </span>
        }
      />

      <FilesDrawer
        threadId={threadId}
        workspaceDir={thread?.workspace_dir}
        running={live.running}
        writtenCount={live.filesWritten}
        open={filesOpen}
        onClose={() => setFilesOpen(false)}
      />

      <Modal
        open={renaming !== null}
        title="重命名对话"
        okText="保存"
        cancelText="取消"
        confirmLoading={rename.isPending}
        onCancel={() => setRenaming(null)}
        onOk={() =>
          rename.mutate((renaming ?? "").trim() || (thread?.title ?? ""), {
            onSuccess: () => setRenaming(null),
            onError: (e) => message.error(`没有保存成功：${e.message}`),
          })
        }
      >
        <Input value={renaming ?? ""} onChange={(e) => setRenaming(e.target.value)} maxLength={200} autoFocus />
      </Modal>
    </>
  );
}
