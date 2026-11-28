"use client";

/** 项目 · 流程：本项目的流程列表；读写成员能发起（选模板、写标题） */
import { Button, Segmented, Select, Table } from "antd";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { StartFlowModal } from "@/components/StartFlowModal";
import { Crumb, Empty, Icon, LoadError, Loading, NodeStatusTag, Page, PageHead, ProcessStatusTag, ProgressBar, useNameOf, Who } from "@/components/ui";
import { ago, STAGE } from "@/lib/format";
import { useProcesses, useProject, useTodos } from "@/lib/queries";
import { useCan, useFollowTeam } from "@/lib/team";
import type { ProcessRow } from "@/lib/types";

export default function ProjectFlows() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const project = useProject(id);
  useFollowTeam(project.data?.team_id);
  const can = useCan();
  const [status, setStatus] = useState("running");
  const [tpl, setTpl] = useState<string>("all");
  const rows = useProcesses(id, status);
  const todos = useTodos();
  const [starting, setStarting] = useState(false);
  const nameOf = useNameOf((rows.data ?? []).map((r) => r.started_by));

  if (project.error) return <LoadError error={project.error} onRetry={() => void project.refetch()} />;
  if (!project.data) return <Loading />;
  const p = project.data;
  const tplName = Object.fromEntries(p.bindings.map((b) => [b.template.id, b.template.name]));
  const list = (rows.data ?? []).filter((r) => tpl === "all" || r.template_id === tpl);

  const columns = [
    { title: "编号", dataIndex: "code", width: 90, render: (v: string) => <span className="mono">{v}</span> },
    {
      title: "标题",
      width: 240,
      render: (_: unknown, r: ProcessRow) => (
        <>
          <b>{r.title}</b>
          <div className="xs faint row" style={{ gap: 4 }}>
            <Who id={r.started_by} name={nameOf(r.started_by)} size={15} /> 发起 · {ago(r.started_at)}
          </div>
        </>
      ),
    },
    {
      title: "当前进展",
      render: (_: unknown, r: ProcessRow) => {
        if (r.status !== "running") return <ProcessStatusTag status={r.status} />;
        const act = r.nodes.filter((n) => !["pending", "passed"].includes(n.status));
        if (!act.length) return <span className="sm muted">等首节点开始</span>;
        return (
          <span className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            {act.map((n) => (
              <span key={n.id} className="row" style={{ gap: 4 }}>
                <b className="sm">{n.name}</b>
                <NodeStatusTag status={n.status} />
              </span>
            ))}
          </span>
        );
      },
    },
    { title: "各节点", width: 130, render: (_: unknown, r: ProcessRow) => <ProgressBar nodes={r.nodes} /> },
    { title: "模板", width: 160, render: (_: unknown, r: ProcessRow) => <span className="sm">{tplName[r.template_id] ?? "—"}</span> },
    {
      title: "等你",
      width: 140,
      render: (_: unknown, r: ProcessRow) => {
        const mine = (todos.data ?? []).filter((t) => t.process_id === r.id);
        if (!mine.length) return <span className="faint sm">—</span>;
        return mine.map((t) => (
          <Link
            key={t.node_id + t.kind}
            href={`/flows/${r.id}/n/${t.node_id}`}
            onClick={(e) => e.stopPropagation()}
            className="rtag"
            style={t.kind === "admit" ? { background: "#f5edff", color: "#7e22ce" } : { background: "#fef3e2", color: "#b45309" }}
            title={`${t.node_name} 等你${STAGE[t.kind]}评审`}
          >
            {STAGE[t.kind]}评审
          </Link>
        ));
      },
    },
  ];

  return (
    <Page>
      <Crumb items={[[p.name, undefined], ["流程", undefined]]} />
      <PageHead
        title="流程"
        sub={
          <>
            <b style={{ color: "var(--fg-2)" }}>{p.name}</b>
            {p.description && <span>· {p.description}</span>}
          </>
        }
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
        <Select
          style={{ width: 180 }}
          value={tpl}
          onChange={setTpl}
          options={[{ value: "all", label: "全部模板" }, ...p.bindings.map((b) => ({ value: b.template.id, label: b.template.name }))]}
        />
        <span className="sp" />
        <span className="sm muted">{list.length} 条</span>
      </div>
      {rows.data && !list.length ? (
        <div className="card">
          <Empty icon="share" title="没有这类流程">
            {p.bindings.length
              ? can.run
                ? "点右上角「发起流程」：选模板、写标题就行"
                : "读写成员可以发起流程"
              : "项目还没绑定流程模板：团队管理员到「配置」里绑定"}
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
          onRow={(r) => ({ onClick: () => router.push(`/flows/${r.id}`) })}
        />
      )}
      {starting && <StartFlowModal project={p} onClose={() => setStarting(false)} />}
    </Page>
  );
}
