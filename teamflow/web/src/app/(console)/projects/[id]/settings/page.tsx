"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Breadcrumb, Button, Card, Form, Input, Popconfirm, Select, Space, Spin, Table, Tag } from "antd";
import Link from "next/link";
import { use, useState } from "react";

import { EmptyBox, PageHead, UserPicker } from "@/components/common";
import { api, errorText } from "@/lib/api";
import { RULE } from "@/lib/format";
import type { FlowTemplate, ProjectDetail, RoleRow, TeamDetail } from "@/lib/types";

export default function ProjectSettings({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const qc = useQueryClient();
  const { message } = App.useApp();
  const { data: p, error } = useQuery({ queryKey: ["project", id], queryFn: () => api.get<ProjectDetail>(`/projects/${id}`) });
  const { data: team } = useQuery({
    queryKey: ["team", p?.team_id],
    queryFn: () => api.get<TeamDetail>(`/teams/${p!.team_id}`),
    enabled: !!p,
  });
  const { data: tpls = [] } = useQuery({
    queryKey: ["flow-templates", "active"],
    queryFn: () => api.get<FlowTemplate[]>("/flow-templates", { status: "active" }),
    enabled: !!p?.can.configure,
  });
  if (error) return <EmptyBox text={errorText(error)} />;
  if (!p) return <Spin />;
  const editable = p.can.configure && p.status === "active";

  async function save(body: Record<string, unknown>) {
    try {
      const r = await api.patch<ProjectDetail>(`/projects/${id}`, { ...body, version: p!.version });
      qc.setQueryData(["project", id], r);
      await qc.invalidateQueries({ queryKey: ["team", p!.team_id] });
      message.success("已保存");
    } catch (e) {
      message.error(errorText(e));
    }
  }
  async function archive(on: boolean) {
    try {
      const r = await api.post<ProjectDetail>(`/projects/${id}/${on ? "archive" : "unarchive"}`);
      qc.setQueryData(["project", id], r);
      await qc.invalidateQueries({ queryKey: ["team", p!.team_id] });
    } catch (e) {
      message.error(errorText(e));
    }
  }

  return (
    <>
      <Breadcrumb
        items={[
          { title: <Link href="/teams">团队</Link> },
          { title: <Link href={`/teams/${p.team_id}`}>{p.team.name}</Link> },
          { title: <Link href={`/projects/${p.id}`}>{p.name}</Link> },
          { title: "项目设置" },
        ]}
        style={{ marginBottom: 12 }}
      />
      <PageHead title="项目设置" sub={p.can.configure ? undefined : "只有团队管理员可以修改项目设置"} />
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        {p.status === "archived" && <Alert type="info" showIcon message="项目已归档：不能发起新流程、不能修改分配" />}
        <Card title="基本信息" size="small">
          <Form
            layout="vertical"
            key={p.version}
            initialValues={{ name: p.name, description: p.description }}
            onFinish={save}
            disabled={!editable}
            style={{ maxWidth: 520 }}
          >
            <Form.Item name="name" label="项目名称" rules={[{ required: true }]}>
              <Input maxLength={64} />
            </Form.Item>
            <Form.Item name="description" label="说明">
              <Input.TextArea maxLength={500} rows={2} />
            </Form.Item>
            {editable && (
              <Button type="primary" htmlType="submit">
                保存
              </Button>
            )}
          </Form>
        </Card>
        <Card title="流程模板" size="small">
          <Space direction="vertical" style={{ width: "100%" }}>
            <div className="tf-muted" style={{ fontSize: 12.5 }}>
              项目跟随模板的当前版本；更换只影响之后新发起的流程。
            </div>
            <Space>
              <Select
                style={{ width: 360 }}
                value={p.flow_template_id ?? undefined}
                disabled={!editable}
                onChange={(v) => save({ flow_template_id: v })}
                options={[
                  ...tpls
                    .filter((t) => t.current_version)
                    .map((t) => ({ value: t.id, label: `${t.icon ?? "🧭"} ${t.name} · ${t.current_version!.label}` })),
                  ...(p.flow_template_id && !tpls.some((t) => t.id === p.flow_template_id)
                    ? [{ value: p.flow_template_id, label: `${p.template_name}（已停用）` }]
                    : []),
                ]}
              />
              {p.roles.template && <Tag>当前 {p.roles.template.version}</Tag>}
              {p.roles.template?.status === "disabled" && <Tag color="orange">模板已停用，建议更换</Tag>}
            </Space>
          </Space>
        </Card>
        <Card
          title={
            <Space>
              角色分配
              <Tag color={p.roles.incomplete ? "red" : "green"}>
                {p.roles.roles.length - p.roles.incomplete}/{p.roles.roles.length} 完成
              </Tag>
            </Space>
          }
          size="small"
        >
          <div className="tf-muted" style={{ fontSize: 12.5, marginBottom: 10 }}>
            执行角色：至少 1 人 + 恰好 1 个可用的团队 Agent（人下达指令、评审成果；Agent 执行）。评审角色：至少 1 人，只能分配人。
            约束不满足不阻止发起流程，但会在项目首页提示。
          </div>
          <RoleTable project={p} team={team} editable={editable} />
        </Card>
        {p.can.archive && (
          <Card title="危险操作" size="small" styles={{ header: { color: "#dc2626" } }}>
            {p.status === "active" ? (
              <Popconfirm title="归档后不能发起新流程，进行中的流程可继续完成。确认归档？" onConfirm={() => archive(true)}>
                <Button danger>归档项目</Button>
              </Popconfirm>
            ) : (
              <Button onClick={() => archive(false)}>取消归档</Button>
            )}
          </Card>
        )}
      </Space>
    </>
  );
}

function RoleTable({ project, team, editable }: { project: ProjectDetail; team?: TeamDetail; editable: boolean }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [editing, setEditing] = useState<string>();
  const [draft, setDraft] = useState<{ users: string[]; agents: string[] }>({ users: [], agents: [] });
  const members = (team?.members.holders ?? []).map((h) => h.user);
  const agents = team?.agents ?? [];

  async function save(role: string) {
    try {
      const r = await api.put<ProjectDetail>(`/projects/${project.id}/roles/${encodeURIComponent(role)}`, {
        ...draft,
        version: project.version,
      });
      qc.setQueryData(["project", project.id], r);
      await qc.invalidateQueries({ queryKey: ["team", project.team_id] });
      setEditing(undefined);
    } catch (e) {
      message.error(errorText(e));
    }
  }

  return (
    <Table<RoleRow>
      rowKey={(r) => `${r.kind}-${r.name}`}
      size="small"
      pagination={false}
      dataSource={project.roles.roles}
      rowClassName={(r) => (r.problems.length ? "tf-row-bad" : "")}
      columns={[
        {
          title: "角色",
          width: 170,
          render: (_, r) => (
            <Space direction="vertical" size={2}>
              <Tag color={r.kind === "exec" ? "blue" : "purple"}>
                {r.kind === "exec" ? "⚙️ 执行" : "✅ 评审"} · {r.name}
              </Tag>
              <span className="tf-muted" style={{ fontSize: 11.5 }}>
                {r.nodes.map((n) => `${n.node}${n.as === "执行" ? "" : `（${n.as}${n.rule ? "，" + RULE[n.rule] : ""}）`}`).join("、")}
              </span>
            </Space>
          ),
        },
        {
          title: "人",
          render: (_, r) =>
            editing === r.name ? (
              <UserPicker value={draft.users} onChange={(users) => setDraft({ ...draft, users })} options={members} />
            ) : (
              <Space wrap size={4}>
                {r.users.map((u) => (
                  <Tag key={u.id} color={u.is_member ? undefined : "red"}>
                    {u.name}
                  </Tag>
                ))}
              </Space>
            ),
        },
        {
          title: "Agent",
          width: 220,
          render: (_, r) =>
            r.kind === "review" ? (
              <span className="tf-muted">—</span>
            ) : editing === r.name ? (
              <Select
                allowClear
                style={{ width: "100%" }}
                value={draft.agents[0]}
                onChange={(v) => setDraft({ ...draft, agents: v ? [v] : [] })}
                options={agents.map((a) => ({ value: a.id, label: `🤖 ${a.name}${a.available ? "" : "（不可用）"}` }))}
                placeholder="选择 1 个团队 Agent"
              />
            ) : (
              r.agents.map((a) => (
                <Tag key={a.id} color={a.available ? "cyan" : "red"}>
                  🤖 {a.name}
                </Tag>
              ))
            ),
        },
        {
          title: "状态",
          width: 150,
          render: (_, r) =>
            r.problems.length ? (
              r.problems.map((x) => (
                <Tag key={x} color="red">
                  {x}
                </Tag>
              ))
            ) : (
              <Tag color="green">已满足</Tag>
            ),
        },
        {
          title: "",
          width: 120,
          render: (_, r) =>
            !editable ? null : editing === r.name ? (
              <Space size={4}>
                <Button size="small" type="primary" onClick={() => save(r.name)}>
                  保存
                </Button>
                <Button size="small" onClick={() => setEditing(undefined)}>
                  取消
                </Button>
              </Space>
            ) : (
              <Button
                size="small"
                onClick={() => {
                  setDraft({ users: r.users.map((u) => u.id), agents: r.agents.map((a) => a.id) });
                  setEditing(r.name);
                }}
              >
                分配
              </Button>
            ),
        },
      ]}
    />
  );
}
