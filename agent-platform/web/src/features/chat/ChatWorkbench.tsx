"use client";

/**
 * 对话页：对话区占满（会话列表在左侧菜单的「最近对话」里，文件是按需打开的抽屉）。
 *
 * 对话本身（消息流、发送、取消、实时轨迹）在共享组件 ThreadConversation 里；这里只放
 * Atlas 自己的东西：头部的文件 / 上下文 / 更多、发送前上传文件、回答反馈。
 *
 * ★ 本机 CLI 会话（thread.device）：头部显示「设备名 · 本机路径」和在线状态；电脑不在线时
 *   一轮还在跑就提示「连上后接着显示」，没在跑就禁用输入框并说明原因；文件抽屉换成本机文件。
 */
import { useQuery } from "@tanstack/react-query";
import { Alert, App as AntdApp, Button, Dropdown, Input, Modal } from "antd";
import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";

import { type ThreadDevice, deviceStatusText, useDeviceChanges } from "@/api/devices";
import { qk } from "@/api/keys";
import { uploadThreadFile, useDeleteThread, useRenameThread, useThread } from "@/api/threads";
import type { Thread } from "@/api/types";
import { Icon } from "@/components/Icon";
import { http } from "@/lib/http";
import { BackfillNotice } from "@/features/memory/BackfillNotice";
import { ThreadConversation, type ConversationStreamState } from "@atlas/chat-ui/src/chat/ThreadConversation";
import type { ContextUsageData } from "@atlas/chat-ui/src/lib/events";

import { ContextMeter } from "./ContextMeter";
import { DeviceFilesDrawer } from "./DeviceFiles";
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
  const thread = threadQ.data;
  const device = thread?.device ?? undefined;
  // 本机会话：电脑在不在线跟着变（重新连上后自动恢复，不用刷新）
  useQuery({
    queryKey: qk.threads.detail(threadId),
    queryFn: () => http.get<Thread>(`/v1/threads/${threadId}`),
    enabled: Boolean(device),
    refetchInterval: 15_000,
  });
  const workspaceCount = useFileCount(threadId, live.running && !device);
  // 本机会话的文件数 = 还没提交的改动数
  const changesQ = useDeviceChanges(threadId, device?.status === "online", live.running);
  const fileCount = device ? (changesQ.data?.entries.length ?? 0) : workspaceCount;
  const deviceDown = device && device.status !== "online" && device.status !== "unknown";

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
        // 业务应用建的会话只读：续聊要回到那个应用，不能绕过它的流程状态（内网身份传递 §7.3）。
        // 本机会话的电脑不在线、且没有在跑的一轮：输入框禁用并说明原因
        readOnly={!!thread?.source_app || Boolean(deviceDown && !live.running)}
        readOnlyHint={
          thread?.source_app
            ? `这个对话来自 ${appName(thread.source_app)}，请回到 ${appName(thread.source_app)} 里继续`
            : device && deviceDownHint(device)
        }
        // 会话后补记写了什么，下次见到用户时告诉他（doc/memory-design.html §4.3）
        notice={
          <>
            <BackfillNotice linkToPage />
            {deviceDown && live.running && device.status === "offline" && (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 8 }}
                message={`和你的电脑「${device.name}」的连接断了：这一轮还在电脑上进行，连上后接着显示`}
                description="不用做什么，电脑重新连上平台后自动接上；也可以点停止结束这一轮。"
              />
            )}
          </>
        }
        headerExtra={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            {device && <DeviceChip device={device} />}
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

      {device ? (
        <DeviceFilesDrawer
          threadId={threadId}
          device={device}
          running={live.running}
          open={filesOpen}
          onClose={() => setFilesOpen(false)}
        />
      ) : (
        <FilesDrawer
          threadId={threadId}
          workspaceDir={thread?.workspace_dir}
          running={live.running}
          writtenCount={live.filesWritten}
          open={filesOpen}
          onClose={() => setFilesOpen(false)}
        />
      )}

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

/** 对话头部：跑在哪台电脑、哪个目录（建好后不能换），在不在线 */
function DeviceChip({ device }: { device: ThreadDevice }) {
  const online = device.status === "online";
  return (
    <span
      title={`跑在「${device.name}」的 ${device.project_dir}（${deviceStatusText(device.status)}；建好后不能换）`}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        height: 24,
        padding: "0 8px",
        borderRadius: 6,
        border: "1px solid var(--line)",
        fontSize: 12,
        maxWidth: 360,
        whiteSpace: "nowrap",
        minWidth: 0,
      }}
    >
      <span
        aria-hidden="true"
        style={{
          width: 7,
          height: 7,
          borderRadius: "50%",
          flex: "none",
          background: online ? "var(--success, #16a34a)" : "var(--fg-4, #b5b5c3)",
        }}
      />
      <Icon name="laptop" size={13} />
      <span style={{ flex: "none" }}>{device.name}</span>
      <span
        style={{
          fontFamily: "var(--mono)",
          color: "var(--fg-3)",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {device.project_dir}
      </span>
    </span>
  );
}

function deviceDownHint(device: ThreadDevice): string {
  return device.status === "revoked"
    ? `这个对话所在的电脑「${device.name}」已被吊销，只能查看对话记录`
    : `你的电脑「${device.name}」不在线：在电脑上启动连接器（atlas-connect run）后就能接着对话`;
}
