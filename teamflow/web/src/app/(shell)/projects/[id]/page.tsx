"use client";

/**
 * 项目 · 流程：本项目的流程列表。等我处理的排最前、整行标出来；每行说清到哪一步了、谁在负责、多久没动。
 * 读写成员能发起（选模板、写标题）。
 */
import { Button, Checkbox, Segmented, Select, Table } from "antd";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { StartFlowModal } from "@/components/StartFlowModal";
import { AgentChip, Empty, Icon, LoadError, Loading, NodeStatusTag, Page, PageHead, ProcessStatusTag, ProgressBar, useNameOf, Who } from "@/components/ui";
import { ago, STAGE } from "@/lib/format";
import { useMe, useProcesses, useProject, useTeamAgents, useTodos } from "@/lib/queries";
import { useCan, useFollowTeam } from "@/lib/team";
import type { ProcessRow } from "@/lib/types";

const DONE = new Set(["pending", "passed"]);

export default function ProjectFlows() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const project = useProject(id);
  useFollowTeam(project.data?.team_id);
  const can = useCan();
  const me = useMe();
  const [status, setStatus] = useState("running");
  const [tpl, setTpl] = useState<string>("all");
  const [mineOnly, setMineOnly] = useState(false);
  const rows = useProcesses(id, status);
  const todos = useTodos();
  const agents = useTeamAgents(project.data?.team_id);
  const [starting, setStarting] = useState(false);
  const nameOf = useNameOf((rows.data ?? []).flatMap((r) => [r.started_by, ...r.nodes.flatMap((n) => n.executors)]));

  if (project.error) return <LoadError error={project.error} onRetry={() => void project.refetch()} />;
  if (!project.data) return <Loading />;
  const p = project.data;
  const uid = me.data?.id;
  const tplName = Object.fromEntries(p.bindings.map((b) => [b.template.id, b.template.name]));
  const agentName = Object.fromEntries((agents.data ?? []).map((a) => [a.atlas_agent_id, a.name]));
  const mineOf = (r: ProcessRow) => (todos.data ?? []).filter((t) => t.process_id === r.id);
  /** 现在在做的步骤；一步都没开始就是第一步 */
  const current = (r: ProcessRow) => {
    const act = r.nodes.filter((n) => !DONE.has(n.status));
    return act.length ? act : r.nodes.slice(0, 1);
  };
  const related = (r: ProcessRow) => r.started_by === uid || mineOf(r).length > 0 || r.nodes.some((n) => uid && n.executors.includes(uid));
  const list = (rows.data ?? [])
    .filter((r) => tpl === "all" || r.template_id === tpl)
    .filter((r) => !mineOnly || related(r))
    .sort((a, b) => Number(mineOf(b).length > 0) - Number(mineOf(a).length > 0));

  const columns = [
    {
      title: "流程",
      width: 300,
      render: (_: unknown, r: ProcessRow) => (
        <>
          <span className="mono muted">{r.code}</span> <b>{r.title}</b>
          <div className="row" style={{ gap: 6, marginTop: 3, flexWrap: "wrap" }}>
            {mineOf(r).map((t) => (
              <span key={t.node_id + t.kind} className="rtag exit">
                待你{STAGE[t.kind]}评审
              </span>
            ))}
            <span className="xs faint">
              {nameOf(r.started_by)} 发起 · {tplName[r.template_id] ?? "—"}
            </span>
          </div>
        </>
      ),
    },
    {
      title: "进展",
      render: (_: unknown, r: ProcessRow) => {
        if (r.status !== "running") return <ProcessStatusTag status={r.status} />;
        const passed = r.nodes.filter((n) => n.status === "passed").length;
        const cur = current(r);
        if (cur.every((n) => n.status === "pending"))
          return (
            <span className="row" style={{ gap: 6 }}>
              <NodeStatusTag status="pending" />
              <span className="sm muted">等上传资料、开始第一步</span>
            </span>
          );
        return (
          <span className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            {cur
              .filter((n) => n.status !== "pending")
              .map((n) => (
                <span key={n.id} className="row" style={{ gap: 4 }}>
                  <span className="sm">{n.name}</span>
                  <NodeStatusTag status={n.status} />
                </span>
              ))}
            <span className="xs muted">
              {passed} / {r.nodes.length}
            </span>
            <ProgressBar nodes={r.nodes} />
          </span>
        );
      },
    },
    {
      title: "负责",
      width: 220,
      render: (_: unknown, r: ProcessRow) => {
        if (r.status !== "running") return <span className="faint sm">—</span>;
        const cur = current(r);
        const people = [...new Set(cur.flatMap((n) => n.executors))];
        const bots = [...new Set(cur.map((n) => (n.atlas_agent_id ? agentName[n.atlas_agent_id] : undefined)).filter((x): x is string => !!x))];
        return (
          <span className="row sm" style={{ flexWrap: "wrap", gap: 6 }}>
            {people.map((u) => (
              <Who key={u} id={u} name={nameOf(u)} size={18} />
            ))}
            {bots.map((b) => (
              <AgentChip key={b} name={b} size={18} />
            ))}
          </span>
        );
      },
    },
    { title: "更新", width: 100, render: (_: unknown, r: ProcessRow) => <span className="xs muted">{ago(r.updated_at ?? r.started_at)}</span> },
  ];

  return (
    <Page>
      <PageHead
        title={p.name}
        sub={p.description ?? undefined}
        extra={
          can.run && (
            <Button type="primary" icon={<Icon name="plus" size={14} />} onClick={() => setStarting(true)}>
              发起流程
            </Button>
          )
        }
      />
      <div className="row" style={{ marginBottom: 12 }}>
        <Segmented
          value={status}
          onChange={(v) => setStatus(String(v))}
          options={[
            { value: "running", label: "进行中" },
            { value: "completed", label: "已完成" },
            { value: "terminated", label: "已终止" },
            { value: "all", label: "全部" },
          ]}
        />
        {p.bindings.length > 1 && (
          <Select
            style={{ width: 180 }}
            value={tpl}
            onChange={setTpl}
            options={[{ value: "all", label: "全部模板" }, ...p.bindings.map((b) => ({ value: b.template.id, label: b.template.name }))]}
          />
        )}
        <Checkbox checked={mineOnly} onChange={(e) => setMineOnly(e.target.checked)}>
          只看与我有关
        </Checkbox>
        <span className="sp" />
        <span className="sm muted">{list.length} 条</span>
      </div>
      {rows.data && !list.length ? (
        <div className="card">
          <Empty icon="share" title={mineOnly ? "没有与你有关的流程" : "没有这类流程"}>
            {p.bindings.length ? (
              can.run ? (
                <Button type="primary" style={{ marginTop: 8 }} onClick={() => setStarting(true)}>
                  发起流程
                </Button>
              ) : (
                "读写成员可以发起流程"
              )
            ) : can.configFlow ? (
              <Button style={{ marginTop: 8 }} onClick={() => router.push(`/projects/${p.id}/config`)}>
                去配置流程模板
              </Button>
            ) : (
              "项目还没配流程模板，等团队管理员配"
            )}
          </Empty>
        </div>
      ) : (
        <Table
          className="table-click"
          rowKey="id"
          size="middle"
          loading={rows.isPending}
          columns={columns}
          dataSource={list}
          pagination={false}
          rowClassName={(r) => (mineOf(r).length ? "row-todo" : "")}
          onRow={(r) => ({ onClick: () => router.push(`/flows/${r.id}`) })}
        />
      )}
      {starting && <StartFlowModal project={p} onClose={() => setStarting(false)} />}
    </Page>
  );
}
