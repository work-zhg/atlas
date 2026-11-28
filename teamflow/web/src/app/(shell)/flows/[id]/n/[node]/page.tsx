"use client";

/**
 * 步骤页（后端叫节点）：顶部是步骤条和「谁负责」，左边是和数字员工的对话，右边是交付 / 参考资料 / 说明 / 评审。
 *
 * ★ 对话组件直连 Agent 平台：看对话、回答提问、审批、停止都不经 TeamFlow；
 *   只有开新一轮（负责人下指令）经 TeamFlow（submitRun），TeamFlow 记下 run 和步骤的对应。
 * ★ 系统发的开工说明、退回意见、继续说明画成卡片（按 run 认，见 instructions），不当成谁说的话。
 * ★ 只有负责人、步骤在进行中才能下指令；其余时候对话只读（卡片也只读）。
 * ★ 暂停数字员工（后端叫接管）：它停下，人直接改文件；改完「让它继续」并留一句话。
 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Input, Tooltip } from "antd";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useState, type ReactNode } from "react";

import { AtlasChatTheme, ThreadConversation, type Message, type SubmitRun } from "@atlas/chat-ui";

import { NodePanel } from "@/components/NodePanel";
import { AgentChip, Empty, Icon, LoadError, Loading, NodeStatusTag, StepNo, Who } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { MATERIALS_DIR } from "@/lib/format";
import { useFlow } from "@/lib/flow";
import { useNode } from "@/lib/queries";
import { isRoot, nodeIds, stepNos } from "@/lib/structure";
import type { Instruction, NodeDetail, ProcessDetail, ProcessNode } from "@/lib/types";

export default function StepPage() {
  const { id, node: nid } = useParams<{ id: string; node: string }>();
  const { proc, events, nameOf, agentOf } = useFlow(id);
  const detail = useNode(id, nid);

  if (proc.error) return <LoadError error={proc.error} />;
  if (!proc.data) return <Loading />;
  const p = proc.data;
  const node = p.nodes.find((n) => n.id === nid);
  if (!node) return <LoadError error={new Error("没有这一步")} />;
  const s = p.structure;
  const no = stepNos(s);
  const agent = agentOf(node);
  const execRole = s.roles[node.exec_role] ?? node.exec_role;
  const lastReturn = [...(events.data ?? [])].reverse().find((e) => e.node_id === nid && (e.type === "node.returned" || e.type === "node.started"));

  return (
    <div className="steppage">
      <div className="stephead">
        <div className="row" style={{ gap: 12, minWidth: 0 }}>
          <Link href={`/flows/${p.id}`} className="row sm" style={{ gap: 4, whiteSpace: "nowrap" }}>
            <Icon name="left" size={14} />
            {p.title}
          </Link>
          <span className="faint">|</span>
          <nav className="stepper" aria-label="步骤">
            {nodeIds(s).map((x, i) => {
              const n = p.nodes.find((y) => y.id === x);
              return (
                <span key={x} className="row" style={{ gap: 6 }}>
                  {i > 0 && <span className="ln" />}
                  <Link href={`/flows/${p.id}/n/${x}`} className="s" aria-current={x === nid ? "step" : undefined}>
                    <StepNo n={no[x]} status={n?.status} />
                    {s.nodes[x]!.name}
                  </Link>
                </span>
              );
            })}
          </nav>
        </div>
        <div className="row" style={{ marginTop: 10, flexWrap: "wrap" }}>
          <h2 style={{ whiteSpace: "nowrap" }}>{node.name}</h2>
          <NodeStatusTag status={node.status} />
          {node.round > 0 && <span className="rtag">第 {node.round} 轮</span>}
          <span className="sm muted row" style={{ gap: 6, marginLeft: 6 }}>
            负责：
            {node.executors.map((u) => (
              <Who key={u} id={u} name={nameOf(u)} size={18} />
            ))}
            {!node.executors.length && <span className="faint">没配人</span>}
            <span className="faint">（{execRole}）</span>
            {agent && (
              <>
                +
                <AgentChip name={agent} size={18} />
              </>
            )}
          </span>
          <span className="sp" />
          <PauseButton process={p} node={node} />
        </div>
        {lastReturn?.type === "node.returned" && node.status === "working" && (
          <div className="banner danger" style={{ marginTop: 10 }}>
            <Icon name="retry" size={15} />
            <div>
              <b>被下一步退回</b>：{nameOf(lastReturn.actor)} 在「{s.nodes[String(lastReturn.data.by)]?.name}」没让它开始 —{" "}
              {String(lastReturn.data.comment ?? "")}
            </div>
          </div>
        )}
        {node.session.state === "taken_over" && node.status === "working" && <Paused process={p} node={node} nameOf={nameOf} agent={agent} />}
      </div>
      <div className="nodepage">
        <div className="left">
          <Conversation process={p} node={node} detail={detail.data} agent={agent} nameOf={nameOf} />
        </div>
        <div className="right">
          <NodePanel process={p} node={node} detail={detail.data} nameOf={nameOf} agent={agent} />
        </div>
      </div>
    </div>
  );
}

function useNodeAction(process: ProcessDetail, node: ProcessNode) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      await qc.invalidateQueries({ queryKey: ["process", process.id] });
      await qc.invalidateQueries({ queryKey: ["node", process.id, node.id] });
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return { busy, act };
}

/** 暂停数字员工：只有负责人、进行中、数字员工在做时 */
function PauseButton({ process: p, node }: { process: ProcessDetail; node: ProcessNode }) {
  const { busy, act } = useNodeAction(p, node);
  if (!node.i_am_executor || p.status !== "running" || node.status !== "working" || node.session.state !== "active") return null;
  return (
    <Tooltip title="数字员工停下，你直接改文件（在「文件」里上传新版本），改完让它继续">
      <Button size="small" icon={<Icon name="stop" size={13} />} loading={busy} onClick={() => void act(() => api.post(`/processes/${p.id}/nodes/${node.id}/takeover`))}>
        暂停数字员工
      </Button>
    </Tooltip>
  );
}

/** 暂停中：负责人改完留一句话、让它继续；别人只看到谁暂停了 */
function Paused({ process: p, node, nameOf, agent }: { process: ProcessDetail; node: ProcessNode; nameOf: (id: string | null | undefined) => string; agent: string | null }) {
  const { busy, act } = useNodeAction(p, node);
  const [note, setNote] = useState("");
  return (
    <div className="banner warn" style={{ marginTop: 10 }}>
      <Icon name="stop" size={15} />
      <div className="grow">
        <b>{nameOf(node.session.taken_by)} 暂停了{agent ?? "数字员工"}</b>
        {node.i_am_executor ? (
          <>
            ：你可以直接改文件（到 <Link href={`/flows/${p.id}/space?dir=${encodeURIComponent(node.name)}`}>文件</Link> 里上传新版本），改完让它继续
            <div className="row" style={{ marginTop: 8 }}>
              <Input
                size="small"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                maxLength={2000}
                placeholder="给它留一句话（可选）：改了什么、接下来做什么"
              />
              <Button
                size="small"
                type="primary"
                icon={<Icon name="play" size={13} />}
                loading={busy}
                onClick={() => void act(() => api.post(`/processes/${p.id}/nodes/${node.id}/handback`, { note }).then(() => setNote("")))}
              >
                让它继续
              </Button>
            </div>
          </>
        ) : (
          "，改完会让它继续"
        )}
      </div>
    </div>
  );
}

const SYSTEM: Partial<Record<Instruction["kind"], string>> = { kickoff: "开工说明", revise: "这一轮的要求", handback: "继续说明" };

/** 系统发给数字员工的话（开工说明、退回意见、继续说明）：一张默认收起的卡片 */
function SystemCard({ kind, text, agent, by }: { kind: Instruction["kind"]; text: string; agent: string | null; by: string | null }) {
  const [open, setOpen] = useState(false);
  const first = text.split("\n").find((l) => l.trim()) ?? "";
  return (
    <div className="syscard" onClick={() => setOpen((v) => !v)}>
      <div className="row" style={{ gap: 6 }}>
        <Icon name={open ? "down" : "right"} size={13} />
        <b>{SYSTEM[kind]}</b>
        <span className="faint">· {by ? `${by} 让它继续时说的` : `系统发给${agent ?? "数字员工"}`}</span>
        {!open && <span className="faint ellipsis grow">{first}</span>}
      </div>
      {open && <div style={{ marginTop: 8, whiteSpace: "pre-wrap" }}>{text}</div>}
    </div>
  );
}

function Conversation({
  process: p,
  node,
  detail,
  agent,
  nameOf,
}: {
  process: ProcessDetail;
  node: ProcessNode;
  detail: NodeDetail | undefined;
  agent: string | null;
  nameOf: (id: string | null | undefined) => string;
}) {
  const { busy, act } = useNodeAction(p, node);
  const thread = node.session.thread_id;
  const mine = node.i_am_executor;

  // 开新一轮经 TeamFlow：它记下 run 和步骤的对应，幂等键就是对话组件给的那个
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

  // 系统发的话：按 run 认（指令记了它开的 run）
  const instructions = detail?.instructions;
  const renderUserMessage = useMemo(() => {
    const byRun = new Map((instructions ?? []).filter((i) => i.run_id && SYSTEM[i.kind]).map((i) => [i.run_id!, i]));
    return (m: Message, text: string): ReactNode | undefined => {
      const ins = m.run_id ? byRun.get(m.run_id) : undefined;
      if (!ins) return undefined;
      return <SystemCard kind={ins.kind} text={text} agent={agent} by={ins.kind === "handback" ? nameOf(ins.author) : null} />;
    };
  }, [instructions, agent, nameOf]);

  const hint = (() => {
    if (p.status !== "running") return `流程${p.status === "completed" ? "已完成" : "已终止"}，对话只能看`;
    if (node.status === "exit_review") return "在评审中，数字员工暂停了。要改得由评审人「退回修改」";
    if (node.status === "exited" || node.status === "passed") return "这一步已准出。要改只能由下一步的人退回";
    if (node.status === "returned") return "被下一步退回了：等上一步重做后再开始";
    if (node.session.state === "taken_over") return `${nameOf(node.session.taken_by)} 暂停了数字员工，让它继续后才能下指令`;
    if (!mine) return `负责人是 ${node.executors.map(nameOf).join("、") || "—"}，你可以看，不能下指令`;
    return undefined;
  })();

  if (thread)
    return (
      <AtlasChatTheme style={{ flex: 1, minHeight: 0, borderRadius: 0 }}>
        <ThreadConversation
          key={thread}
          threadId={thread}
          hideHeader
          hideModePicker
          readOnly={!node.session.can_send}
          readOnlyHint={hint}
          submitRun={submitRun}
          renderUserMessage={renderUserMessage}
          placeholder={`给「${agent ?? "数字员工"}」下指令、回答它的问题…`}
        />
      </AtlasChatTheme>
    );
  if (node.status === "pending") {
    if (!isRoot(p.structure, node.id))
      return <Empty icon="clock" title="还没开始">上一步准出{p.structure.nodes[node.id]?.admit ? "、你们审过它交付的文件" : ""}后，{agent ?? "数字员工"}会自动开始</Empty>;
    return (
      <Empty icon="play" title="还没开始">
        先把资料传到 <Link href={`/flows/${p.id}/space?dir=${encodeURIComponent(MATERIALS_DIR)}`}>文件 · 资料</Link>
        ，开始后{agent ?? "数字员工"}会先读这些，有不清楚的会在这里问
        {mine && p.status === "running" ? (
          <div style={{ marginTop: 12 }}>
            <Button type="primary" loading={busy} onClick={() => void act(() => api.post(`/processes/${p.id}/nodes/${node.id}/begin`))}>
              开始这一步
            </Button>
          </div>
        ) : (
          <div style={{ marginTop: 8 }}>等 {node.executors.map(nameOf).join("、") || "负责人"} 开始</div>
        )}
      </Empty>
    );
  }
  if (node.status === "admit_review") return <Empty icon="shield" title="等准入评审">审过上一步交付的文件后开始</Empty>;
  return <Empty icon="chat" title="这一步还没有对话" />;
}
