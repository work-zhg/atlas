"use client";

import { PlusOutlined, SyncOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App,
  Breadcrumb,
  Button,
  Card,
  Form,
  Input,
  List,
  Modal,
  Popconfirm,
  Progress,
  Radio,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tabs,
  Tag,
} from "antd";
import Link from "next/link";
import { use, useState } from "react";

import { AgentAvatar, DeptPicker, EmptyBox, LevelTag, PageHead, PersonAvatar, StatusTag, UserPicker } from "@/components/common";
import { api, ApiError, errorText } from "@/lib/api";
import { dt } from "@/lib/format";
import type { FlowTemplate, Grant, TeamDetail } from "@/lib/types";

export default function TeamPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data: t, isLoading, error } = useQuery({
    queryKey: ["team", id],
    queryFn: () => api.get<TeamDetail>(`/teams/${id}`),
  });
  if (error) return <EmptyBox text={errorText(error)} />;
  if (isLoading || !t) return <Spin />;

  return (
    <>
      <Breadcrumb items={[{ title: <Link href="/teams">团队</Link> }, { title: t.name }]} style={{ marginBottom: 12 }} />
      <PageHead
        title={
          <Space>
            {t.name}
            {t.my_level && t.my_level !== "NONE" && <LevelTag level={t.my_level} />}
            {t.status === "archived" && <StatusTag status="archived" />}
          </Space>
        }
        sub={t.description || undefined}
        extra={t.can.edit && <EditTeam team={t} />}
      />
      <Tabs
        items={[
          { key: "projects", label: `项目 ${t.projects.length}`, children: <Projects team={t} /> },
          { key: "members", label: `成员 ${t.members.holders.length}`, children: <Members team={t} /> },
          { key: "agents", label: `Agent ${t.agents.length}`, children: <Agents team={t} /> },
        ]}
      />
    </>
  );
}

function EditTeam({ team }: { team: TeamDetail }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  async function submit() {
    const v = await form.validateFields();
    try {
      await api.patch(`/teams/${team.id}`, { ...v, version: team.version });
      await qc.invalidateQueries({ queryKey: ["team", team.id] });
      message.success("已保存");
      setOpen(false);
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <>
      <Button onClick={() => setOpen(true)}>编辑团队</Button>
      <Modal open={open} title="编辑团队" onOk={submit} onCancel={() => setOpen(false)} destroyOnHidden>
        <Form form={form} layout="vertical" initialValues={{ name: team.name, description: team.description }}>
          <Form.Item name="name" label="团队名称" rules={[{ required: true }]}>
            <Input maxLength={64} />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input.TextArea maxLength={500} rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

// ───────────────────────────── 项目

function Projects({ team }: { team: TeamDetail }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      {team.can.create && (
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)} style={{ marginBottom: 16 }}>
          新建项目
        </Button>
      )}
      {!team.projects.length ? (
        <EmptyBox text="还没有项目" />
      ) : (
        <div className="tf-card-grid">
          {team.projects.map((p) => {
            const c = p.completion ?? { total: 0, done: 0 };
            return (
              <Link key={p.id} href={`/projects/${p.id}`} style={{ textDecoration: "none" }}>
                <Card hoverable style={{ opacity: p.status === "archived" ? 0.65 : 1 }}>
                  <Space style={{ width: "100%", justifyContent: "space-between" }}>
                    <span style={{ fontSize: 15, fontWeight: 650 }}>📁 {p.name}</span>
                    {p.status === "archived" && <StatusTag status="archived" />}
                  </Space>
                  <div className="tf-muted" style={{ margin: "6px 0 12px", minHeight: 20, fontSize: 13 }}>
                    {p.description || "—"}
                  </div>
                  <div style={{ fontSize: 12.5 }} className="tf-muted">
                    🧭 {p.template_name ?? "未绑定模板"} {p.template_version}
                  </div>
                  <div style={{ fontSize: 12.5, marginTop: 8 }} className="tf-muted">
                    角色分配 {c.done}/{c.total}
                    <Progress percent={c.total ? Math.round((c.done / c.total) * 100) : 0} size="small" showInfo={false} />
                  </div>
                </Card>
              </Link>
            );
          })}
        </div>
      )}
      <CreateProject team={team} open={open} onClose={() => setOpen(false)} />
    </>
  );
}

function CreateProject({ team, open, onClose }: { team: TeamDetail; open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const { data: tpls = [] } = useQuery({
    queryKey: ["flow-templates", "active"],
    queryFn: () => api.get<FlowTemplate[]>("/flow-templates", { status: "active" }),
    enabled: open,
  });
  async function submit() {
    const v = await form.validateFields();
    try {
      const p = await api.post<{ id: string }>(`/teams/${team.id}/projects`, v);
      await qc.invalidateQueries({ queryKey: ["team", team.id] });
      message.success("项目已创建，接下来分配角色");
      onClose();
      window.location.href = `/projects/${p.id}/settings`;
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open={open} title="新建项目" onOk={submit} onCancel={onClose} destroyOnHidden>
      <Form form={form} layout="vertical" requiredMark={false}>
        <Form.Item name="name" label="项目名称" rules={[{ required: true, message: "请填写项目名称" }]}>
          <Input maxLength={64} placeholder="如：账号体系升级" />
        </Form.Item>
        <Form.Item name="description" label="说明">
          <Input.TextArea maxLength={500} rows={2} />
        </Form.Item>
        <Form.Item name="flow_template_id" label="流程模板" rules={[{ required: true, message: "请选择流程模板" }]}>
          <Select
            placeholder="只能绑定已发布、未停用的模板"
            options={tpls
              .filter((t) => t.current_version)
              .map((t) => ({ value: t.id, label: `${t.icon ?? "🧭"} ${t.name} · ${t.current_version!.label}` }))}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}

// ───────────────────────────── 成员

function Members({ team }: { team: TeamDetail }) {
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const [open, setOpen] = useState(false);
  const manage = team.members.can_manage;
  const refresh = () => qc.invalidateQueries({ queryKey: ["team", team.id] });

  async function toggleAdmin(g: Grant, admin: boolean) {
    try {
      const r = await api.patch<{ warnings: string[] }>(`/teams/${team.id}/members/${g.id}`, { admin });
      if (r.warnings.length) modal.warning({ title: "请注意", content: r.warnings.join("\n") });
      await refresh();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  async function remove(g: Grant) {
    try {
      const r = await api.del<{ removed_assignments: { project_name: string; role: string }[] }>(
        `/teams/${team.id}/members/${g.id}`,
      );
      if (r.removed_assignments.length)
        modal.info({
          title: "已一并移除角色分配",
          content: r.removed_assignments.map((a) => `${a.project_name} · ${a.role}`).join("、"),
        });
      await refresh();
    } catch (e) {
      message.error(errorText(e));
    }
  }

  return (
    <Space direction="vertical" size={20} style={{ width: "100%" }}>
      <Card
        title="授权"
        size="small"
        extra={
          manage && (
            <Button type="primary" size="small" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
              添加成员
            </Button>
          )
        }
      >
        <div className="tf-muted" style={{ fontSize: 12.5, marginBottom: 8 }}>
          成员与团队管理员存于用户中心「团队」数据权限：成员 = 读写，团队管理员 = Owner。可以按个人或部门授权。
        </div>
        <Table<Grant>
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={team.members.grants}
          columns={[
            {
              title: "对象",
              render: (_, g) => (
                <Space>
                  {g.subject.type === "USER" ? <PersonAvatar name={g.subject.name} size={24} /> : "🏢"}
                  {g.subject.name}
                  {g.subject.account && <span className="tf-muted">{g.subject.account}</span>}
                  {g.subject.type === "DEPT" && g.include_sub && <Tag>含下级</Tag>}
                </Space>
              ),
            },
            { title: "级别", width: 130, render: (_, g) => <LevelTag level={g.permission} /> },
            {
              title: "团队管理员",
              width: 110,
              render: (_, g) => (
                <Switch
                  size="small"
                  checked={g.permission === "OWNER"}
                  disabled={!manage}
                  onChange={(v) => toggleAdmin(g, v)}
                />
              ),
            },
            {
              title: "",
              width: 70,
              render: (_, g) =>
                manage && (
                  <Popconfirm title="移除该授权？其在项目中的角色分配会一并移除" onConfirm={() => remove(g)}>
                    <Button type="link" size="small" danger>
                      移除
                    </Button>
                  </Popconfirm>
                ),
            },
          ]}
        />
      </Card>
      <Card title={`实际成员 ${team.members.holders.length}`} size="small">
        <div className="tf-muted" style={{ fontSize: 12.5, marginBottom: 8 }}>
          部门授权展开后的人员（已停用的不显示）
        </div>
        <List
          size="small"
          dataSource={team.members.holders}
          renderItem={(h) => (
            <List.Item>
              <Space>
                <PersonAvatar name={h.user.name} size={24} />
                {h.user.name}
                <span className="tf-muted">{h.user.account}</span>
                <LevelTag level={h.permission} />
              </Space>
              <Space wrap>
                {(h.roles ?? []).map((r) => (
                  <Tag key={r}>{r}</Tag>
                ))}
              </Space>
            </List.Item>
          )}
        />
      </Card>
      <AddMembers team={team} open={open} onClose={() => setOpen(false)} />
    </Space>
  );
}

function AddMembers({ team, open, onClose }: { team: TeamDetail; open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const [form] = Form.useForm();
  async function submit() {
    const v = await form.validateFields();
    const subjects = [
      ...(v.users ?? []).map((id: string) => ({ type: "USER", id })),
      ...(v.depts ?? []).map((id: string) => ({ type: "DEPT", id, include_sub: v.include_sub })),
    ];
    if (!subjects.length) return message.warning("请选择成员");
    try {
      const r = await api.post<{ warnings: string[] }>(`/teams/${team.id}/members`, { subjects, admin: v.level === "OWNER" });
      if (r.warnings.length) modal.warning({ title: "请注意", content: r.warnings.join("\n") });
      await qc.invalidateQueries({ queryKey: ["team", team.id] });
      message.success("已添加");
      form.resetFields();
      onClose();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open={open} title="添加成员" onOk={submit} onCancel={onClose} destroyOnHidden width={540}>
      <Form form={form} layout="vertical" initialValues={{ level: "WRITE", include_sub: true }}>
        <Form.Item name="users" label="个人">
          <UserPicker />
        </Form.Item>
        <Form.Item name="depts" label="部门">
          <DeptPicker />
        </Form.Item>
        <Form.Item name="include_sub" label="部门含下级" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item name="level" label="身份">
          <Radio.Group
            options={[
              { value: "WRITE", label: "成员（读写）" },
              { value: "OWNER", label: "团队管理员（Owner）" },
            ]}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}

// ───────────────────────────── Agent

function Agents({ team }: { team: TeamDetail }) {
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const [open, setOpen] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const refresh = () => qc.invalidateQueries({ queryKey: ["team", team.id] });

  async function sync() {
    setSyncing(true);
    try {
      await api.post(`/teams/${team.id}/agents/sync`);
      await refresh();
      message.success("已按 Atlas 同步");
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setSyncing(false);
    }
  }
  async function remove(agentId: string, confirm = false) {
    try {
      await api.del(`/teams/${team.id}/agents/${agentId}`, { confirm });
      await refresh();
    } catch (e) {
      if (e instanceof ApiError && e.code === "AGENT_IN_USE") {
        const usage = (e.details.usage as { project_name: string; role: string }[]) ?? [];
        modal.confirm({
          title: "该 Agent 正在项目中担任执行角色",
          content: `确认后将从这些分配中移除：${usage.map((u) => `${u.project_name} · ${u.role}`).join("、")}`,
          okButtonProps: { danger: true },
          okText: "移出并移除分配",
          onOk: () => remove(agentId, true),
        });
      } else message.error(errorText(e));
    }
  }

  return (
    <>
      <Space style={{ marginBottom: 16 }}>
        {team.can.agent && (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
            接入 Agent
          </Button>
        )}
        <Button icon={<SyncOutlined spin={syncing} />} onClick={sync}>
          同步可用性
        </Button>
      </Space>
      {!team.agents.length ? (
        <EmptyBox text="还没有接入 Agent。Agent 在 Atlas 中创建与配置，这里只引用" />
      ) : (
        <List
          bordered
          style={{ background: "#fff" }}
          dataSource={team.agents}
          renderItem={(a) => (
            <List.Item
              actions={
                team.can.agent
                  ? [
                      <Button key="rm" type="link" danger size="small" onClick={() => remove(a.id)}>
                        移出
                      </Button>,
                    ]
                  : []
              }
            >
              <List.Item.Meta
                avatar={<AgentAvatar name={a.name} available={a.available} size={36} />}
                title={
                  <Space>
                    {a.name}
                    {a.available ? <Tag color="green">可用</Tag> : <Tag color="red">不可用</Tag>}
                    {a.model && <span className="tf-mono tf-muted">{a.model}</span>}
                  </Space>
                }
                description={
                  <>
                    <div>{a.description || "—"}</div>
                    <Space wrap style={{ marginTop: 4 }}>
                      {(a.roles ?? []).map((r) => (
                        <Tag key={r}>{r}</Tag>
                      ))}
                      <span className="tf-muted" style={{ fontSize: 12 }}>
                        同步于 {dt(a.synced_at)}
                      </span>
                    </Space>
                  </>
                }
              />
            </List.Item>
          )}
        />
      )}
      <AddAgent team={team} open={open} onClose={() => setOpen(false)} />
    </>
  );
}

function AddAgent({ team, open, onClose }: { team: TeamDetail; open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [q, setQ] = useState("");
  const { data = [], isLoading, error } = useQuery({
    queryKey: ["agent-candidates", team.id, q],
    queryFn: () =>
      api.get<{ id: string; name: string; description: string | null; model: string | null; added: boolean }[]>(
        `/teams/${team.id}/agent-candidates`,
        { q },
      ),
    enabled: open,
  });
  async function add(id: string) {
    try {
      await api.post(`/teams/${team.id}/agents`, { atlas_agent_id: id });
      await qc.invalidateQueries({ queryKey: ["team", team.id] });
      await qc.invalidateQueries({ queryKey: ["agent-candidates", team.id] });
      message.success("已接入");
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open={open} title="从 Atlas 接入 Agent" footer={null} onCancel={onClose} destroyOnHidden width={620}>
      <Input.Search placeholder="搜索 Agent 名称" allowClear onSearch={setQ} style={{ marginBottom: 12 }} />
      {error ? (
        <EmptyBox text={errorText(error)} />
      ) : (
        <List
          loading={isLoading}
          dataSource={data}
          locale={{ emptyText: "Atlas 中没有已启用的 Agent" }}
          renderItem={(a) => (
            <List.Item
              actions={[
                a.added ? (
                  <Tag key="added">已接入</Tag>
                ) : (
                  <Button key="add" size="small" type="primary" onClick={() => add(a.id)}>
                    接入
                  </Button>
                ),
              ]}
            >
              <List.Item.Meta avatar={<AgentAvatar name={a.name} />} title={a.name} description={a.description || a.model} />
            </List.Item>
          )}
        />
      )}
    </Modal>
  );
}
