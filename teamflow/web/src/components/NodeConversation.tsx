"use client";

/**
 * 人机协同对话区：直接用 Atlas 的对话组件（@atlas/chat-ui，与 Atlas 对话页同一份实现）。
 *
 * ★ 接口根是本节点的 Atlas 会话代理（后端 api/atlas_proxy.py）：只放行这个节点的会话，
 *   读需要团队成员、写（发指令 / 取消 / 审批）需要执行人 —— 前端只读只是体验，权限在服务端。
 * ★ Atlas 的对话升级（新的事件类型、卡片、交互）后，TeamFlow 重新构建即跟随，不维护第二套。
 * ★ 没有上游的节点开工时不启动 Agent：执行人第一次说明要做什么后才建会话（不空跑）。
 *   还没有会话时这里给一个输入框，发出后会话建好，自动切换成对话组件。
 */
import { AtlasChatTheme, AtlasClientProvider, ThreadConversation, type AtlasClientConfig } from "@atlas/chat-ui";
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Empty, Input, Space } from "antd";
import { useMemo, useState } from "react";

import { api, BASE_PATH, errorText, writeHeaders } from "@/lib/api";
import type { NodeDetail, ProcessDetail } from "@/lib/types";

export function NodeConversation({ process, node }: { process: ProcessDetail; node: NodeDetail }) {
  const client = useMemo<AtlasClientConfig>(
    () => ({
      apiBase: `${BASE_PATH}/api/v1/processes/${process.id}/nodes/${node.node_id}/atlas`,
      headers: writeHeaders,
    }),
    [process.id, node.node_id],
  );

  const thread = node.atlas_thread_id;
  if (!thread) {
    const hasAgent = Boolean(process.nodes[node.node_id]?.agent);
    const waiting = node.status === "working" && hasAgent && node.messages.length === 0;
    if (waiting && node.can.handle) return <FirstInstruction process={process} node={node} />;
    if (waiting)
      return <Empty style={{ padding: "40px 0" }} description={`等执行角色「${node.config.exec_role}」的成员告诉 Agent 要做什么`} />;
    return (
      <Empty
        style={{ padding: "40px 0" }}
        description={
          node.status === "pending"
            ? "节点还没有开工"
            : hasAgent
              ? "正在建立 Agent 会话…"
              : "这个节点没有可用的执行 Agent：请手工提交产物"
        }
      />
    );
  }

  const hint = node.can.handle
    ? undefined
    : node.status !== "working"
      ? `节点${node.stage}：对话只读`
      : `只有执行角色「${node.config.exec_role}」的成员可以给 Agent 下指令`;

  return (
    <AtlasClientProvider config={client}>
      <AtlasChatTheme style={{ height: 640 }}>
        <ThreadConversation key={thread} threadId={thread} readOnly={!node.can.handle} readOnlyHint={hint} />
      </AtlasChatTheme>
    </AtlasClientProvider>
  );
}

/** 第一条说明：发出后才启动 Agent（开工上下文由后端拼在前面）。 */
function FirstInstruction({ process, node }: { process: ProcessDetail; node: NodeDetail }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  async function send() {
    setBusy(true);
    try {
      await api.post(`/processes/${process.id}/nodes/${node.node_id}/messages`, { text });
      setText("");
      await qc.invalidateQueries({ queryKey: ["node", process.id, node.node_id] });
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Space direction="vertical" style={{ width: "100%", padding: "16px 0" }}>
      <div style={{ fontSize: 13.5 }}>
        告诉 Agent 这个节点要做什么。Agent 在你发出后开始工作（会自动带上流程、产物路径、文件模板与提交方式）。
      </div>
      <Input.TextArea
        value={text}
        onChange={(e) => setText(e.target.value)}
        autoSize={{ minRows: 4, maxRows: 12 }}
        maxLength={20000}
        placeholder={`如：${node.config.output_name}要覆盖……，参考……`}
      />
      <Button type="primary" loading={busy} disabled={!text.trim()} onClick={send}>
        发送给 Agent
      </Button>
    </Space>
  );
}
