"use client";

/**
 * 智能平台首页（同 ChatGPT / Claude）：一个输入框，发出第一句话就新建对话并跳过去。
 *
 * ★ 默认用平台设置里的默认智能体；?agent=<id>（智能体列表点「对话」）或输入框里临时换。
 *   都没有时退回第一个能用的智能体；一个都没有就告诉用户去哪里建。
 * ★ 添加的文件在会话建好之后再上传（会话之前没有工作区），消息里附上它们的路径。
 */
import { App as AntdApp, Button, Result, Skeleton } from "antd";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { useAgent, useAgents, useDefaultAgent } from "@/api/agents";
import { uploadThreadFile, useCreateThread, withAttachments } from "@/api/threads";
import type { ApprovalMode } from "@/lib/events";
import { can, useMe } from "@/lib/session";
import { selectableModels, useAgentModels } from "@atlas/chat-ui/src/api/models";
import { useSendMessage } from "@atlas/chat-ui/src/api/runs";
import { Composer } from "@atlas/chat-ui/src/chat/Composer";
import { ModelPicker } from "@atlas/chat-ui/src/chat/ModelPicker";
import { ModePicker } from "@atlas/chat-ui/src/chat/ModePicker";
import { newIdempotencyKey } from "@atlas/chat-ui/src/lib/id";

import { AgentPicker } from "./AgentPicker";
import styles from "./home.module.css";

function greeting(): string {
  const h = new Date().getHours();
  return h < 6 ? "夜深了" : h < 11 ? "早上好" : h < 14 ? "中午好" : h < 18 ? "下午好" : "晚上好";
}

/** 默认智能体的快捷问题：点一下填进输入框，不直接发 */
const QUICK = ["整理本周的工作周报", "分析一份表格数据", "帮我写一个脚本", "查一下公司的报销制度"];

export function HomePage() {
  const router = useRouter();
  const params = useSearchParams();
  const { message } = AntdApp.useApp();
  const { data: me } = useMe();
  const defaultQ = useDefaultAgent();
  const agentsQ = useAgents({ status: "enabled" });
  const createThread = useCreateThread();
  const sendMessage = useSendMessage();

  const [picked, setPicked] = useState<string>();
  const [mode, setMode] = useState<ApprovalMode | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [fill, setFill] = useState<{ text: string; seq: number }>();

  const agents = agentsQ.data?.data ?? [];
  const defaultId = defaultQ.data?.id;
  const agentId = picked ?? params.get("agent") ?? defaultId ?? agents[0]?.id;
  const agentQ = useAgent(agentId);
  const agent = agents.find((a) => a.id === agentId) ?? agentQ.data;
  // spec 在接口里是自由 dict：只取这里要的几项（CLI 智能体没有审批模式，权限由 CLI 自己管；
  // 模型在拉起 CLI 时就定了，对话里不能换）
  const spec = agentQ.data?.spec as
    | {
        kind?: string;
        model?: { model: string };
        approval?: { default_mode: ApprovalMode; allowed_modes: ApprovalMode[] };
      }
    | undefined;
  const isCli = spec?.kind === "cli";
  const approval = isCli ? undefined : spec?.approval;
  const modelsQ = useAgentModels(agentId, Boolean(spec) && !isCli);

  const send = async (text: string, files: File[]) => {
    if (!agentId) return;
    try {
      const thread = await createThread.mutateAsync({ agent_id: agentId, title: "" });
      const paths = await Promise.all(files.map((f) => uploadThreadFile(thread.id, f)));
      await sendMessage.mutateAsync({
        threadId: thread.id,
        content: [{ type: "text", text: withAttachments(text, paths) }],
        idempotencyKey: newIdempotencyKey(),
        approvalMode: mode ?? undefined,
        model: model ?? undefined,
      });
      router.push(`/chat/${thread.id}`);
    } catch (e) {
      message.error((e as Error).message);
      throw e;
    }
  };

  if (defaultQ.isPending || agentsQ.isPending) {
    return (
      <div className={styles.hero}>
        <Skeleton active style={{ maxWidth: 640 }} />
      </div>
    );
  }

  if (!agentId) {
    return (
      <div className={styles.hero}>
        <Result
          status="info"
          title="还没有可以对话的智能体"
          subTitle={
            can(me, "config:manage")
              ? "在「系统 › 平台设置」里一键创建通用助手，所有人进来就能直接用。"
              : "创建一个智能体，或者请管理员设置默认智能体。"
          }
          extra={
            <Button type="primary" onClick={() => router.push(can(me, "config:manage") ? "/system" : "/agents")}>
              {can(me, "config:manage") ? "去设置" : "去创建"}
            </Button>
          }
        />
      </div>
    );
  }

  return (
    <div className={styles.hero}>
      <h1 className={styles.title}>
        {greeting()}，{me?.name ?? ""}
      </h1>
      <div className={styles.box}>
        <Composer
          bare
          size="large"
          autoFocus
          running={false}
          acceptFiles
          placeholder="有什么可以帮你？"
          fill={fill}
          onSend={send}
          toolbar={
            <>
              <AgentPicker
                agents={agents}
                value={agent}
                defaultId={defaultId}
                onChange={(id) => {
                  setPicked(id);
                  setMode(null);
                  setModel(null);
                }}
              />
              {spec?.model && (
                <ModelPicker
                  value={model ?? spec.model.model}
                  defaultModel={spec.model.model}
                  models={selectableModels(modelsQ.data)}
                  onChange={setModel}
                  locked={isCli ? "CLI 智能体不能在对话里换模型，请到智能体配置里修改" : undefined}
                />
              )}
              {approval && (
                <ModePicker
                  value={mode ?? approval.default_mode}
                  allowed={approval.allowed_modes}
                  onChange={setMode}
                />
              )}
            </>
          }
        />
      </div>
      {agentId === defaultId && (
        <div className={styles.quick}>
          {QUICK.map((q) => (
            <button
              key={q}
              type="button"
              className={styles.chip}
              onClick={() => setFill({ text: q, seq: (fill?.seq ?? 0) + 1 })}
            >
              {q}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
