"use client";

/**
 * 流程 · 概览：先回答「现在怎样、要我做什么」（现在卡片 / 开始引导），再是流程图、各步骤交付的文件、
 * 最近的里程碑、参与的人。
 *
 * ★ 发起后各步骤都未开始：第一步的负责人先把资料传进来，再点「开始第一步」，数字员工才动手。
 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Upload } from "antd";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { FlowGraph, type NodeState } from "@/components/FlowGraph";
import { FlowHeader } from "@/components/FlowHeader";
import { AgentChip, FileChip, Icon, LoadError, Loading, Page, StepNo, Who } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { describe } from "@/lib/events";
import { ago, MATERIALS_DIR, NODE_STATUS, STAGE } from "@/lib/format";
import { useFlow, useUpload } from "@/lib/flow";
import { useSpace, useTodos } from "@/lib/queries";
import { isRoot, nodeIds, stepNos, upstreams } from "@/lib/structure";
import { useCan } from "@/lib/team";
import type { ProcessDetail, ProcessNode } from "@/lib/types";

/** 进行中的步骤（不算未开始、已完成） */
const ACTIVE = new Set(["working", "exit_review", "admit_review", "exited", "returned"]);

export default function FlowOverview() {
  const { id } = useParams<{ id: string }>();
  const flow = useFlow(id);
  const { proc, events, nameOf, agentOf, nodeName, version } = flow;
  const todos = useTodos();
  const can = useCan();

  if (proc.error) return <LoadError error={proc.error} onRetry={() => void proc.refetch()} />;
  if (!proc.data) return <Loading />;
  const p = proc.data;
  const s = p.structure;
  const no = stepNos(s);
  const byId = Object.fromEntries(p.nodes.map((n) => [n.id, n]));
  const people = (n: ProcessNode) => n.executors.map((u) => ({ id: u, name: nameOf(u) }));
  const states: Record<string, NodeState> = Object.fromEntries(
    p.nodes.map((n) => [n.id, { status: n.status, round: n.round, agent: agentOf(n), people: people(n) }]),
  );
  const delivered = nodeIds(s).filter((n) => byId[n]?.latest_submission);
  const recent = (events.data ?? [])
    .map((e) => ({ e, m: describe(e, nameOf, nodeName) }))
    .filter((x) => x.m)
    .slice(-6)
    .reverse();

  return (
    <Page>
      <FlowHeader process={p} tab="overview" version={version} nameOf={nameOf} />

      <Now process={p} mine={(todos.data ?? []).filter((t) => t.process_id === p.id)} flow={flow} />

      <div className="card" style={{ marginTop: 14 }}>
        <FlowGraph structure={s} states={states} href={(nid) => `/flows/${p.id}/n/${nid}`} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)", gap: 14, marginTop: 14 }}>
        <div className="card pad">
          <div className="row" style={{ marginBottom: 6 }}>
            <h2>交付文件</h2>
            <span className="sp" />
            <Link className="sm" href={`/flows/${p.id}/space`}>
              全部文件
            </Link>
          </div>
          {delivered.length ? (
            delivered.map((n) => {
              const node = byId[n]!;
              const sub = node.latest_submission!;
              return (
                <div key={n} className="ev" style={{ alignItems: "flex-start", padding: "7px 0", borderBottom: "1px solid var(--tf-line-soft)" }}>
                  <StepNo n={no[n]} status={node.status} />
                  <div className="grow">
                    <b className="sm">{node.name}</b>
                    <div>
                      {sub.paths.map((x) => (
                        <FileChip key={x} processId={p.id} path={x} />
                      ))}
                    </div>
                  </div>
                  <span className="xs faint" style={{ whiteSpace: "nowrap", marginTop: 2 }}>
                    第 {sub.round} 轮 · {NODE_STATUS[node.status].label}
                  </span>
                </div>
              );
            })
          ) : (
            <div className="sm muted">还没有步骤交付过文件</div>
          )}
        </div>
        <div className="card pad">
          <div className="row" style={{ marginBottom: 6 }}>
            <h2>最近</h2>
            <span className="sp" />
            <Link className="sm" href={`/flows/${p.id}/trace`}>
              全部记录
            </Link>
          </div>
          {recent.map(({ e, m }) => (
            <div key={e.seq} className="ev">
              <span className="t">{ago(e.at)}</span>
              <span className="sm">
                {e.node_id && <b>{nodeName(e.node_id)} · </b>}
                {m!.text}
              </span>
            </div>
          ))}
        </div>
      </div>

      <div className="card pad" style={{ marginTop: 14 }}>
        <div className="row" style={{ flexWrap: "wrap", gap: "6px 18px" }}>
          <b className="sm">参与的人</b>
          {Object.entries(s.roles).map(([rk, rn]) => {
            const agent = agentOf({ atlas_agent_id: null, exec_role: rk });
            return (
              <span key={rk} className="row sm" style={{ gap: 6 }}>
                <span className="muted">{rn}</span>
                {(p.roles[rk] ?? []).map((u) => (
                  <Who key={u} id={u} name={nameOf(u)} size={18} />
                ))}
                {!(p.roles[rk] ?? []).length && <span className="faint">没配人</span>}
                {agent && <AgentChip name={agent} size={18} />}
              </span>
            );
          })}
          <span className="sp" />
          {can.configFlow && (
            <Link className="sm" href={`/projects/${p.project_id}/config`}>
              去项目配置
            </Link>
          )}
        </div>
      </div>
    </Page>
  );
}

type Flow = ReturnType<typeof useFlow>;

/** 现在卡片：轮到我的事排最前；还没开始的给开始引导；其余说清哪一步在进行、谁在处理 */
function Now({ process: p, mine, flow }: { process: ProcessDetail; mine: { kind: "admit" | "exit"; node_id: string; since: string }[]; flow: Flow }) {
  const { nameOf, agentOf } = flow;
  const s = p.structure;
  const no = stepNos(s);
  const byId = Object.fromEntries(p.nodes.map((n) => [n.id, n]));
  const go = (nid: string) => `/flows/${p.id}/n/${nid}`;

  if (p.status !== "running") {
    const done = p.status === "completed";
    return (
      <div className={`now ${done ? "done" : "off"}`}>
        <Icon name={done ? "check" : "stop"} size={18} />
        <div className="grow">
          <b>{done ? "流程完成了" : "流程已终止"}</b>
          <div className="sm muted">文件和记录都保留，只能看不能改</div>
        </div>
      </div>
    );
  }

  const cards = [];
  for (const t of mine) {
    const n = byId[t.node_id];
    if (!n) continue;
    const ups = (upstreams(s)[n.id] ?? []).map((u) => byId[u]).filter(Boolean);
    const subs = t.kind === "exit" ? [n.latest_submission] : ups.map((u) => u!.latest_submission);
    const files = subs.flatMap((x) => x?.paths ?? []);
    const by = subs.find((x) => x)?.submitted_by;
    cards.push(
      <div key={`todo-${t.node_id}-${t.kind}`} className="now todo">
        <Icon name="shield" size={18} />
        <div className="grow">
          <b>
            等你{STAGE[t.kind]}评审 ·「{n.name}」{t.kind === "exit" ? `第 ${n.round} 轮` : ""}
          </b>
          <div className="sm muted row" style={{ flexWrap: "wrap", gap: 4 }}>
            {by && <span>{nameOf(by)} 提交了</span>}
            {files.map((f) => (
              <FileChip key={f} processId={p.id} path={f} />
            ))}
            <span>· {ago(t.since)}</span>
          </div>
        </div>
        <Link href={go(n.id)}>
          <Button type="primary">去评审</Button>
        </Link>
      </div>,
    );
  }

  const roots = nodeIds(s).filter((n) => isRoot(s, n) && byId[n]?.status === "pending");
  if (roots.length) return <>{cards}<StartGuide process={p} roots={roots.map((n) => byId[n]!)} flow={flow} /></>;

  for (const n of p.nodes.filter((x) => ACTIVE.has(x.status))) {
    if (mine.some((t) => t.node_id === n.id)) continue;
    const agent = agentOf(n);
    const who = [n.executors.map(nameOf).join("、"), agent].filter(Boolean).join(" + ");
    const myTurn = n.i_am_executor && n.status === "working";
    const line = (() => {
      if (n.status === "working") return n.session.state === "taken_over" ? `${nameOf(n.session.taken_by)} 暂停了数字员工，正在自己改` : `${who} 正在做`;
      if (n.status === "exit_review" || n.status === "admit_review")
        return `等 ${(n.open_review?.reviewers ?? []).map(nameOf).join("、") || "评审人"} 评审`;
      if (n.status === "exited") return "已准出，等下一步的人确认接收";
      return "被下一步退回，等上一步重做";
    })();
    cards.push(
      <div key={n.id} className={`now ${myTurn ? "todo" : ""}`}>
        <StepNo n={no[n.id]} status={n.status} />
        <div className="grow">
          <b>
            {myTurn ? `轮到你：「${n.name}」` : `「${n.name}」${NODE_STATUS[n.status].label}`}
            {n.round > 1 ? ` · 第 ${n.round} 轮` : ""}
          </b>
          <div className="sm muted">{myTurn ? `和${agent ?? "数字员工"}一起做，做完提交评审` : line}</div>
        </div>
        <Link href={go(n.id)}>
          <Button type={myTurn ? "primary" : "default"}>{myTurn ? "去处理" : "查看"}</Button>
        </Link>
      </div>,
    );
  }
  return <>{cards}</>;
}

/** 刚发起的流程：① 传资料（放进「资料」文件夹）② 开始第一步。不是第一步负责人的看到「等谁开始」 */
function StartGuide({ process: p, roots, flow }: { process: ProcessDetail; roots: ProcessNode[]; flow: Flow }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const router = useRouter();
  const { nameOf, agentOf } = flow;
  const upload = useUpload(p.id);
  const materials = useSpace(p.id, MATERIALS_DIR);
  const files = (materials.data?.entries ?? []).filter((e) => !e.is_dir);
  const [busy, setBusy] = useState<string>();
  const mineRoots = roots.filter((n) => n.i_am_executor);

  const begin = async (nid: string) => {
    setBusy(nid);
    try {
      await api.post(`/processes/${p.id}/nodes/${nid}/begin`);
      await qc.invalidateQueries({ queryKey: ["process", p.id] });
      router.push(`/flows/${p.id}/n/${nid}`);
    } catch (e) {
      message.error(errorText(e));
      setBusy(undefined);
    }
  };

  if (!mineRoots.length)
    return (
      <div className="now">
        <Icon name="clock" size={18} />
        <div className="grow">
          <b>还没开始</b>
          <div className="sm muted">
            等 {[...new Set(roots.flatMap((n) => n.executors))].map(nameOf).join("、") || "第一步的负责人"} 上传资料、开始第一步
            {p.can_run && "；你也可以先把手上的资料传到「文件」里"}
          </div>
        </div>
      </div>
    );

  return (
    <div className="guide">
      <b style={{ fontSize: 15 }}>开始第一步{roots.length === 1 ? `「${roots[0]!.name}」` : ""}</b>
      <div className="gstep">
        <StepNo n={1} />
        <div className="grow">
          <b>上传参考资料</b>
          <span className="sm muted">　需求文档、现状说明、截图都行，数字员工会先读这些；没有也可以直接开始</span>
          <Upload.Dragger
            multiple
            showUploadList={false}
            style={{ marginTop: 8, background: "var(--bg-1)" }}
            customRequest={({ file, onSuccess }) => void upload(file as File, MATERIALS_DIR).then(() => onSuccess?.({}))}
          >
            <div className="sm muted" style={{ padding: "4px 0" }}>
              把文件拖到这里，或 <a>选择文件</a>
              <div className="xs faint">单个文件不超过 50 MB</div>
            </div>
          </Upload.Dragger>
          {!!files.length && (
            <div style={{ marginTop: 6 }}>
              {files.map((f) => (
                <FileChip key={f.path} processId={p.id} path={f.path} />
              ))}
            </div>
          )}
        </div>
      </div>
      {mineRoots.map((n) => (
        <div key={n.id} className="gstep">
          <StepNo n={2} />
          <div className="grow">
            <b>开始「{n.name}」</b>
            <div className="sm muted">
              {agentOf(n) ?? "数字员工"}会按工作说明开始做；有不清楚的地方会在对话里问你
            </div>
          </div>
          <Button type="primary" loading={busy === n.id} onClick={() => void begin(n.id)}>
            开始第一步
          </Button>
        </div>
      ))}
    </div>
  );
}
