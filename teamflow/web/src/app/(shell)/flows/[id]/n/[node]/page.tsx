"use client";

/**
 * 节点页：一个节点 = 一个数字员工会话（挂着流程空间）。左边是对话，右边是产出物 / 输入 / 评审。
 *
 * ★ 对话组件直连 Agent 平台：看对话、回答提问、审批、接管浏览器、停止都不经 TeamFlow；
 *   只有开新一轮（执行人发指令）经 TeamFlow（submitRun），TeamFlow 记下 run 和节点的对应。
 * ★ 只有执行人、节点在协同中才能发指令；其余时候对话只读（卡片也只读）。
 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Input, Segmented } from "antd";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { AtlasChatTheme, ThreadConversation, type SubmitRun } from "@atlas/chat-ui";

import { NodePanel } from "@/components/NodePanel";
import { SubmitModal } from "@/components/SubmitModal";
import { Crumb, Empty, Icon, LoadError, Loading, NodeStatusTag } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { STAGE } from "@/lib/format";
import { useFlow } from "@/lib/flow";
import { useNode, useProject } from "@/lib/queries";
import { isRoot, nodeIds } from "@/lib/structure";
import type { ProcessDetail, ProcessNode } from "@/lib/types";

export default function NodePage() {
  const { id, node: nid } = useParams<{ id: string; node: string }>();
  const { proc, events, agents, nameOf, agentName } = useFlow(id);
  const project = useProject(proc.data?.project_id);
  const detail = useNode(id, nid);
  const router = useRouter();

  if (proc.error) return <LoadError error={proc.error} />;
  if (!proc.data) return <Loading />;
  const p = proc.data;
  const node = p.nodes.find((n) => n.id === nid);
  if (!node) return <LoadError error={new Error("没有这个节点")} />;
  const s = p.structure;
  // 开工时锁定的数字员工；还没开工的按项目配置里这个角色当前分配的显示
  const planned = project.data?.bindings.find((b) => b.template.id === p.template_id)?.assignments[node.exec_role]?.agent;
  const agent = agentName(node.atlas_agent_id) ?? agents.data?.find((a) => a.id === planned)?.name ?? null;
  const execRole = s.roles[node.exec_role] ?? node.exec_role;
  const lastReturn = [...(events.data ?? [])].reverse().find((e) => e.node_id === nid && (e.type === "node.returned" || e.type === "node.started"));

  return (
    <div className="nodepage">
      <div className="left">
        <div className="nhead">
          <Crumb
            items={[
              [project.data?.name ?? "项目", `/projects/${p.project_id}`],
              [`${p.code} ${p.title}`, `/flows/${p.id}`],
              [node.name, undefined],
            ]}
          />
          <div className="row">
            <h2 style={{ whiteSpace: "nowrap" }}>{node.name}</h2>
            <NodeStatusTag status={node.status} />
            {node.round > 0 && <span className="rtag">第 {node.round} 轮</span>}
            <span className="sp" />
            <Segmented
              size="small"
              value={nid}
              onChange={(v) => router.push(`/flows/${p.id}/n/${v}`)}
              options={nodeIds(s).map((x) => ({ value: x, label: s.nodes[x]!.name }))}
            />
          </div>
          <div className="locks">
            {node.started_at && agent ? (
              <span className="lock" title="开工时锁定；打回重开不重新锁定">
                <Icon name="lock" size={12} />
                执行的数字员工「{agent}」
              </span>
            ) : (
              <span className="lock">
                <Icon name="clock" size={12} />
                执行的数字员工：开工时锁定
              </span>
            )}
            <Link className="lock" href={`/flows/${p.id}/space`}>
              <Icon name="folder" size={12} />
              流程空间
            </Link>
            {node.open_review && (
              <span className="lock" title="进入评审时锁定">
                <Icon name="shield" size={12} />
                {STAGE[node.open_review.stage]}评审人：{node.open_review.reviewers.map(nameOf).join("、")}
              </span>
            )}
            <span className="lock">
              <Icon name="user" size={12} />
              执行：{execRole} · {node.executors.map(nameOf).join("、") || "—"}
            </span>
          </div>
          {lastReturn?.type === "node.returned" && node.status === "working" && (
            <div className="banner danger" style={{ marginTop: 10 }}>
              <Icon name="retry" size={15} />
              <div>
                <b>被打回</b>：{nameOf(lastReturn.actor)} 在「{s.nodes[String(lastReturn.data.by)]?.name}」准入时打回 — {String(lastReturn.data.comment ?? "")}
              </div>
            </div>
          )}
        </div>
        <Conversation process={p} node={node} agent={agent} nameOf={nameOf} />
      </div>
      <div className="right">
        <NodePanel process={p} node={node} detail={detail.data} nameOf={nameOf} agentName={agentName} />
      </div>
    </div>
  );
}

function Conversation({
  process: p,
  node,
  agent,
  nameOf,
}: {
  process: ProcessDetail;
  node: ProcessNode;
  agent: string | null;
  nameOf: (id: string | null | undefined) => string;
}) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const [submitting, setSubmitting] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const thread = node.session.thread_id;
  const mine = node.i_am_executor;
  const takenOver = node.session.state === "taken_over";

  const refresh = async () => {
    await qc.invalidateQueries({ queryKey: ["process", p.id] });
    await qc.invalidateQueries({ queryKey: ["node", p.id, node.id] });
  };
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      await refresh();
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  // 开新一轮经 TeamFlow：它记下 run 和节点的对应，幂等键就是对话组件给的那个
  const submitRun = useMemo<SubmitRun>(
    () => async (input) => {
      const r = await api.post<{ run_id: string }>(`/processes/${p.id}/nodes/${node.id}/messages`, {
        content: input.content,
        idempotency_key: input.idempotencyKey,
      });
      // 对话组件只用到 run 已经开了；消息号、状态由它自己从会话流里取
      return { run_id: r.run_id, message_id: "", status: "queued" };
    },
    [p.id, node.id],
  );

  const hint = (() => {
    if (p.status !== "running") return `流程${p.status === "completed" ? "已完成" : "已终止"}：对话只读`;
    if (node.status === "exit_review") return "评审中：执行人不能再发指令、数字员工不再写入。要改请评审「驳回修改」";
    if (node.status === "exited" || node.status === "passed") return "节点已准出。要改只能由下游准入时打回";
    if (node.status === "returned") return "已打回：上游重做后重新走准入";
    if (takenOver) return `${nameOf(node.session.taken_by)} 接管了这个节点：数字员工停下，改完交还`;
    if (!mine) return `执行人：${node.executors.map(nameOf).join("、") || "—"}。你可以看，不能发指令`;
    return undefined;
  })();

  let body;
  if (thread) {
    body = (
      <AtlasChatTheme style={{ flex: 1, minHeight: 0, borderRadius: 0 }}>
        <ThreadConversation
          key={thread}
          threadId={thread}
          hideHeader
          readOnly={!node.session.can_send}
          readOnlyHint={hint}
          submitRun={submitRun}
          placeholder={`给「${agent ?? "数字员工"}」下指令、回答它的问题…`}
        />
      </AtlasChatTheme>
    );
  } else if (node.status === "pending") {
    const root = isRoot(p.structure, node.id);
    body = (
      <Empty icon="clock" title="还没开工">
        {root ? (
          <>
            首节点由执行人点「开始」：先把参考资料上传到 <Link href={`/flows/${p.id}/space`}>流程空间</Link>
            ，开工时系统把工作说明和流程标题作为第一条指令发给「{agent ?? "数字员工"}」
            {mine && p.status === "running" && (
              <div style={{ marginTop: 12 }}>
                <Button type="primary" loading={busy} onClick={() => void act(() => api.post(`/processes/${p.id}/nodes/${node.id}/begin`))}>
                  开始
                </Button>
              </div>
            )}
          </>
        ) : (
          "上游准出、通过本节点的准入后开工。开工时系统把工作说明和上游提交的文件路径作为第一条指令发给数字员工；文件都在流程空间里，直接读"
        )}
      </Empty>
    );
  } else if (node.status === "admit_review") {
    body = <Empty icon="shield" title="等准入评审">准入通过后开工</Empty>;
  } else {
    body = <Empty icon="chat" title="这个节点还没有对话" />;
  }

  // 执行人的操作：接管 / 交还、提交评审（对话组件之外）
  const actions =
    mine && p.status === "running" && node.status === "working" ? (
      <div className="nfoot">
        {takenOver ? (
          <div className="col" style={{ gap: 8 }}>
            <Input.TextArea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              autoSize={{ minRows: 1, maxRows: 4 }}
              maxLength={2000}
              placeholder="交还时给数字员工留一句话（可选）：改了什么、接下来做什么"
            />
            <div className="row">
              <span className="xs faint">接管中：数字员工停下，你直接改流程空间里的文件</span>
              <span className="sp" />
              <Button loading={busy} onClick={() => void act(() => api.post(`/processes/${p.id}/nodes/${node.id}/handback`, { note }).then(() => setNote("")))}>
                交还
              </Button>
              <Button type="primary" icon={<Icon name="send" size={14} />} onClick={() => setSubmitting(true)}>
                提交评审
              </Button>
            </div>
          </div>
        ) : (
          <div className="row">
            <Button size="small" loading={busy} onClick={() => void act(() => api.post(`/processes/${p.id}/nodes/${node.id}/takeover`))}>
              接管
            </Button>
            <span className="xs faint">数字员工停下，你直接改流程空间的文件，改完交还</span>
            <span className="sp" />
            <Button size="small" type="primary" icon={<Icon name="send" size={14} />} onClick={() => setSubmitting(true)}>
              提交评审
            </Button>
          </div>
        )}
      </div>
    ) : null;

  return (
    <>
      <div className="nconv">{body}</div>
      {actions}
      {submitting && <SubmitModal process={p} node={node} onClose={() => setSubmitting(false)} />}
    </>
  );
}
