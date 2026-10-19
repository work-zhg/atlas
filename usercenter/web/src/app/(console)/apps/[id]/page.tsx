"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Checkbox, Descriptions, Empty, Form, Input, List, Modal, Radio, Select, Space, Table, Tabs, Tag, Typography } from "antd";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { DeptSelect, LevelTag, PageHead, showOnce, StatusTag, SubjectPicker, toSubjects, UserSelect, type SubjectValue } from "@/components/common";
import { GrantToModal, useGrantMutations } from "@/components/grantActions";
import { api, errorText } from "@/lib/api";
import { fmtTime, LEVEL } from "@/lib/format";
import { useCan } from "@/lib/session";
import type { AppInfo, DataTypeInfo, GrantRow, Level, MenuItem, MenuNode, Operation, Role } from "@/lib/types";

export default function AppDetailPage() {
  const { id } = useParams<{ id: string }>();
  const can = useCan();
  const router = useRouter();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const app = useQuery({ queryKey: ["app", id], queryFn: () => api.get<AppInfo>(`/apps/${id}`) });
  const a = app.data;
  if (!a) return <Card loading />;

  const tabs = [
    // 凭据属于平台（app:manage）；可访问范围属于本租户（tenant_app:manage）
    !a.is_builtin && (can("app:manage") || can("tenant_app:manage")) && { key: "conf", label: "接入配置", children: <ConfTab app={a} /> },
    { key: "ops", label: `操作目录（${a.operations}）`, children: <OpsTab app={a} /> },
    { key: "menus", label: `菜单（${a.menus}）`, children: <MenusTab app={a} /> },
    can("role:view") && { key: "roles", label: `角色（${a.roles}）`, children: <RolesTab app={a} /> },
    !a.is_builtin && can("app:manage") && { key: "dtypes", label: `数据编码（${a.data_types}）`, children: <DataTypesTab app={a} /> },
  ].filter(Boolean) as { key: string; label: string; children: React.ReactNode }[];

  async function toggle() {
    if (!a) return;
    const disabling = a.status === "active";
    modal.confirm({
      title: disabling ? `停用应用 · ${a.name}` : `启用应用 · ${a.name}`,
      content: disabling ? "停用后该应用不能再获取接口令牌，已签发的令牌立即失效；角色、菜单与授权全部保留。" : "启用后应用可重新获取接口令牌。",
      okButtonProps: { danger: disabling },
      onOk: async () => {
        try {
          await api.post(`/apps/${a.id}/${disabling ? "disable" : "enable"}`);
          qc.invalidateQueries({ queryKey: ["app", id] });
          qc.invalidateQueries({ queryKey: ["apps"] });
          message.success(disabling ? "应用已停用" : "应用已启用");
        } catch (e) {
          message.error(errorText(e));
        }
      },
    });
  }

  return (
    <>
      <PageHead
        title={`${a.icon ?? ""} ${a.name}`}
        sub={a.is_builtin ? "本系统 · 内置应用：操作与菜单随版本发布（只读），用户中心的侧边栏就是按这里的菜单和登录人的角色生成的" : `${a.description ?? ""} · 接入于 ${fmtTime(a.created_at)}`}
        extra={
          <Space>
            {!a.is_builtin && can("app:manage") && (
              <Button danger={a.status === "active"} onClick={toggle}>
                {a.status === "active" ? "停用" : "启用"}
              </Button>
            )}
            <Button onClick={() => router.push("/apps")}>← 返回应用列表</Button>
          </Space>
        }
      />
      {a.status === "disabled" && <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="应用已停用：开放接口拒绝该应用的调用。" />}
      <Tabs items={tabs} destroyOnHidden />
    </>
  );
}

// ───────────────────────────────────────────── 接入配置

function ConfTab({ app }: { app: AppInfo }) {
  const can = useCan();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const [all, setAll] = useState(app.scope_all);
  const [depts, setDepts] = useState<string[]>(app.scope_dept_ids ?? []);
  const rotate = () =>
    modal.confirm({
      title: `重新生成 Secret · ${app.name}`,
      content: "旧 Secret 及其签发的接口令牌立即失效，应用在更新配置前无法调用开放接口。",
      okButtonProps: { danger: true },
      onOk: async () => {
        const r = await api.post<{ app: AppInfo; app_secret: string }>(`/apps/${app.id}/rotate-secret`);
        qc.invalidateQueries({ queryKey: ["app", app.id] });
        showOnce(modal, "新的 App Secret", [["App Key", r.app.app_key ?? ""], ["App Secret", r.app_secret]], "App Secret 只显示这一次，只能保存在应用后端。");
      },
    });
  async function saveCode(code: string) {
    try {
      await api.patch(`/apps/${app.id}`, { code: code.trim() || null, version: app.version });
      qc.invalidateQueries({ queryKey: ["app", app.id] });
      qc.invalidateQueries({ queryKey: ["apps"] });
      message.success("已保存应用编码");
    } catch (e) {
      message.error(errorText(e));
    }
  }
  async function saveScope() {
    try {
      const r = await api.put<{ accessible_users: number }>(`/apps/${app.id}/scope`, { all, dept_ids: all ? [] : depts });
      qc.invalidateQueries({ queryKey: ["app", app.id] });
      qc.invalidateQueries({ queryKey: ["apps"] });
      message.success(`已保存，当前 ${r.accessible_users} 人可访问`);
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, alignItems: "start" }}>
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        {can("app:manage") && (
        <Card size="small" title="应用凭据" extra={<Tag color="blue">服务端调用开放接口</Tag>}>
          <Descriptions column={1} size="small" styles={{ label: { width: 100 } }}>
            <Descriptions.Item label="应用编码">
              <Typography.Text
                code
                editable={{
                  tooltip: "内网服务调用开放接口时，请求头 X-Caller-App 写它",
                  onChange: (v) => void saveCode(v),
                }}
              >
                {app.code ?? ""}
              </Typography.Text>
            </Descriptions.Item>
            <Descriptions.Item label="App Key">
              <Typography.Text code copyable>
                {app.app_key}
              </Typography.Text>
            </Descriptions.Item>
            <Descriptions.Item label="App Secret">
              <Space>
                <Typography.Text code>••••••••••••{app.secret_tail}</Typography.Text>
                <Button size="small" onClick={rotate}>
                  重新生成
                </Button>
              </Space>
            </Descriptions.Item>
          </Descriptions>
          <div className="uc-muted">Secret 只在生成时显示一次；只能保存在应用后端，不能下发到浏览器。</div>
        </Card>
        )}
        <Card size="small" title="开放接口">
          <Descriptions column={1} size="small" styles={{ label: { width: 100 } }}>
            <Descriptions.Item label="Base URL">
              <span className="uc-mono">/open/v1</span>
            </Descriptions.Item>
            <Descriptions.Item label="获取令牌">
              <span className="uc-mono">POST /auth/token</span>
              <span className="uc-muted">（带 tenant_code：令牌绑定到该租户）</span>
            </Descriptions.Item>
            <Descriptions.Item label="主要接口">用户与部门查询、授权快照（can_access / 角色 / 操作码 / 菜单）、实时校验、数据权限</Descriptions.Item>
          </Descriptions>
          <Alert type="info" showIcon message="统一登录（SSO）暂不提供：应用自行处理登录，按账号与用户中心对齐身份；以后引入 Keycloak。" />
        </Card>
      </Space>
      <Card size="small" title="可访问范围（本租户）" extra={<span className="uc-muted">当前 {app.accessible_users ?? 0} 人可访问</span>}>
        <Radio.Group value={all} onChange={(e) => setAll(e.target.value)} style={{ marginBottom: 12 }}>
          <Radio value>全员</Radio>
          <Radio value={false}>指定部门（含其子部门）</Radio>
        </Radio.Group>
        {!all && <DeptSelect multiple value={depts} onChange={(v) => setDepts(v as string[])} />}
        {!all && !depts.length && <div style={{ color: "#dc2626", marginTop: 6 }}>未设置部门：无人可访问</div>}
        <div style={{ textAlign: "right", marginTop: 12 }}>
          <Button type="primary" onClick={saveScope}>
            保存范围
          </Button>
        </div>
      </Card>
    </div>
  );
}

// ───────────────────────────────────────────── 操作目录

function OpsTab({ app }: { app: AppInfo }) {
  const can = useCan();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const editable = !app.is_builtin && can("app:manage");
  const ops = useQuery({ queryKey: ["app", app.id, "ops"], queryFn: () => api.get<Operation[]>(`/apps/${app.id}/operations`) });
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const refresh = () => (qc.invalidateQueries({ queryKey: ["app", app.id] }), qc.invalidateQueries({ queryKey: ["role"] }));
  async function create(v: { module: string; code: string; name: string }) {
    try {
      await api.post(`/apps/${app.id}/operations`, v);
      refresh();
      setOpen(false);
      message.success("操作已登记，可在角色中勾选");
    } catch (e) {
      message.error(errorText(e));
    }
  }
  async function remove(o: Operation) {
    const pre = await api.del<{ roles: string[] }>(`/operations/${o.id}`, { dry_run: true });
    modal.confirm({
      title: `删除操作 · ${o.code}`,
      content: pre.roles.length ? `该操作被 ${pre.roles.join("、")} 使用，删除后将从这些角色中移除；应用前端对应的按钮将对所有人隐藏。` : "确定删除？",
      okButtonProps: { danger: true },
      onOk: async () => (await api.del(`/operations/${o.id}`), refresh(), message.success("已删除")),
    });
  }
  const first = ops.data?.[0]?.code ?? "xxx:view";
  return (
    <Card
      size="small"
      title="操作目录"
      extra={
        <Space>
          <span className="uc-muted">该应用可被授权的操作，角色从这里勾选{app.is_builtin ? " · 内置应用只读" : ""}</span>
          {editable && <Button type="primary" size="small" onClick={() => (form.resetFields(), setOpen(true))}>＋ 新增操作</Button>}
        </Space>
      }
    >
      <Table<Operation>
        size="small"
        rowKey="id"
        loading={ops.isLoading}
        pagination={false}
        dataSource={ops.data ?? []}
        columns={[
          { title: "模块", dataIndex: "module" },
          { title: "操作", dataIndex: "name" },
          { title: "操作码", render: (_, o) => <span className="uc-mono">{o.code}</span> },
          { title: "使用该操作的角色", render: (_, o) => (o.roles?.length ? o.roles.map((r) => <Tag key={r}>{r}</Tag>) : <span className="uc-muted">无</span>) },
          { title: "", align: "right", render: (_, o) => editable && <Button type="link" size="small" danger onClick={() => remove(o)}>删除</Button> },
        ]}
      />
      <div className="uc-muted" style={{ margin: "12px 0 6px" }}>
        前端按操作码控制权限（示例）：
      </div>
      <pre className="uc-code">{`// 用户登录应用后，应用后端调用 GET /open/v1/users/{id}/authz 取得本应用的操作码
const can = (op) => permissions.includes(op);
{can('${first}') && <Button>…</Button>}   // 没有权限：按钮不显示
// 后端接口同样按操作码校验，前端控制只负责「看不到」`}</pre>
      <Modal open={open} title={`新增操作 · ${app.name}`} onCancel={() => setOpen(false)} onOk={() => form.submit()} okText="保存">
        <Form form={form} layout="vertical" onFinish={create}>
          <Form.Item name="module" label="模块" rules={[{ required: true }]}>
            <Input placeholder="例如：流程" />
          </Form.Item>
          <Form.Item name="name" label="操作名称" rules={[{ required: true }]}>
            <Input placeholder="例如：撤回流程" />
          </Form.Item>
          <Form.Item name="code" label="操作码" extra="小写字母、数字、下划线，格式「资源:动作」；在应用内唯一，创建后不可改" rules={[{ required: true, pattern: /^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$/, message: "格式：资源:动作" }]}>
            <Input placeholder="process:withdraw" />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}

// ───────────────────────────────────────────── 菜单

function menuRows(items: MenuItem[], parent: string | null = null, depth = 0): (MenuItem & { depth: number })[] {
  return items
    .filter((m) => m.parent_id === parent)
    .sort((a, b) => a.sort - b.sort)
    .flatMap((m) => [{ ...m, depth }, ...menuRows(items, m.id, depth + 1)]);
}

function MenusTab({ app }: { app: AppInfo }) {
  const can = useCan();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const editable = !app.is_builtin && can("app:manage");
  const menus = useQuery({ queryKey: ["app", app.id, "menus"], queryFn: () => api.get<MenuItem[]>(`/apps/${app.id}/menus`) });
  const [form, setForm] = useState<{ open: boolean; menu?: MenuItem | null; parent?: string | null }>({ open: false });
  const [pvUser, setPvUser] = useState<string>();
  const preview = useQuery({
    queryKey: ["app", app.id, "preview", pvUser],
    queryFn: () => api.get<{ can_access: boolean; roles: string[]; menus: MenuNode[] }>(`/apps/${app.id}/menus/preview`, { user_id: pvUser }),
    enabled: !!pvUser,
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["app", app.id] });
  const rows = menuRows(menus.data ?? []);

  async function remove(m: MenuItem) {
    try {
      const pre = await api.del<{ roles: string[] }>(`/menus/${m.id}`, { dry_run: true });
      modal.confirm({
        title: `删除菜单 · ${m.name}`,
        content: pre.roles.length ? `该菜单绑定在 ${pre.roles.join("、")} 上，删除后一并解除。` : "确定删除？",
        okButtonProps: { danger: true },
        onOk: async () => (await api.del(`/menus/${m.id}`), refresh(), message.success("已删除")),
      });
    } catch (e) {
      message.error(errorText(e));
    }
  }

  const renderTree = (nodes: MenuNode[], depth = 0): React.ReactNode =>
    nodes.map((n, i) =>
      n.children ? (
        <div key={`${depth}-${i}`}>
          <div className="uc-pv-dir" style={{ paddingLeft: 10 + depth * 14 }}>
            {n.icon} {n.name}
          </div>
          {renderTree(n.children, depth + 1)}
        </div>
      ) : (
        <div key={n.code} className="uc-pv-item" style={{ paddingLeft: 10 + depth * 14 }}>
          {n.icon} {n.name}
        </div>
      ),
    );

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 340px", gap: 16, alignItems: "start" }}>
      <Card
        size="small"
        title="菜单"
        extra={
          <Space>
            <span className="uc-muted">{app.is_builtin ? "内置菜单随版本发布" : "角色在「角色 → 菜单权限」中绑定"}</span>
            {editable && <Button type="primary" size="small" onClick={() => setForm({ open: true, menu: null, parent: null })}>＋ 新增</Button>}
          </Space>
        }
      >
        <Table
          size="small"
          rowKey="id"
          loading={menus.isLoading}
          pagination={false}
          dataSource={rows}
          columns={[
            { title: "名称", render: (_, m) => <span style={{ paddingLeft: m.depth * 22, whiteSpace: "nowrap" }}>{m.icon} {m.type === "dir" ? <b>{m.name}</b> : m.name}</span> },
            { title: "类型", render: (_, m) => <Space size={4}>{m.type === "dir" ? <Tag>目录</Tag> : <Tag color="blue">菜单</Tag>}{m.is_public && <Tag color="green">公共</Tag>}</Space> },
            { title: "编码", render: (_, m) => <span className="uc-mono">{m.code}</span> },
            { title: "路由", render: (_, m) => <span className="uc-mono">{m.path ?? "—"}</span> },
            { title: "可见", render: (_, m) => <span className="uc-muted">{m.type === "dir" ? "随子菜单" : m.is_public ? "所有人" : `${m.role_count ?? 0} 个角色`}</span> },
            {
              title: "",
              align: "right",
              render: (_, m) =>
                editable && (
                  <Space size={0}>
                    {m.type === "dir" && <Button type="link" size="small" onClick={() => setForm({ open: true, menu: null, parent: m.id })}>＋ 子菜单</Button>}
                    <Button type="link" size="small" onClick={() => api.post(`/menus/${m.id}/move-up`).then(refresh)}>↑</Button>
                    <Button type="link" size="small" onClick={() => setForm({ open: true, menu: m })}>编辑</Button>
                    <Button type="link" size="small" danger onClick={() => remove(m)}>删除</Button>
                  </Space>
                ),
            },
          ]}
        />
      </Card>
      <Card size="small" title="菜单预览">
        <UserSelect value={pvUser} onChange={(v) => setPvUser(v as string)} />
        <div className="uc-pv-side" style={{ marginTop: 10 }}>
          <div className="uc-pv-brand">
            {app.icon} {app.name}
          </div>
          {!pvUser ? (
            <div className="uc-pv-dir">选择一个用户预览其可见菜单</div>
          ) : preview.data && !preview.data.can_access ? (
            <div className="uc-pv-dir">该用户不在可访问范围内</div>
          ) : (
            renderTree(preview.data?.menus ?? [])
          )}
        </div>
        {preview.data && <div className="uc-muted" style={{ marginTop: 8 }}>该用户在本应用的角色：{preview.data.roles.join("、") || "无"}</div>}
        {preview.data && <pre className="uc-code" style={{ marginTop: 8 }}>{JSON.stringify({ can_access: preview.data.can_access, menus: preview.data.menus }, null, 2)}</pre>}
      </Card>
      {form.open && <MenuForm app={app} menu={form.menu ?? null} parent={form.parent ?? null} dirs={(menus.data ?? []).filter((m) => m.type === "dir")} onClose={() => setForm({ open: false })} onDone={refresh} />}
    </div>
  );
}

function MenuForm({ app, menu, parent, dirs, onClose, onDone }: { app: AppInfo; menu: MenuItem | null; parent: string | null; dirs: MenuItem[]; onClose: () => void; onDone: () => void }) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const type = Form.useWatch("type", form) ?? menu?.type ?? "menu";
  async function submit(v: { type: "dir" | "menu"; code: string; name: string; icon?: string; path?: string; is_public?: boolean; parent_id?: string | null }) {
    try {
      if (menu) {
        await api.patch(`/menus/${menu.id}`, { name: v.name, icon: v.icon, parent_id: v.parent_id ?? null, ...(menu.type === "menu" ? { path: v.path, is_public: !!v.is_public } : {}) });
      } else {
        await api.post(`/apps/${app.id}/menus`, { ...v, is_public: !!v.is_public, parent_id: v.parent_id ?? null });
      }
      onDone();
      message.success(!menu && v.type === "menu" && !v.is_public ? "已保存；在角色的「菜单权限」中绑定后才可见" : "已保存");
      onClose();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open title={menu ? `编辑菜单 · ${menu.name}` : `新增菜单 · ${app.name}`} onCancel={onClose} onOk={() => form.submit()} okText="保存">
      <Form
        form={form}
        layout="vertical"
        onFinish={submit}
        initialValues={menu ? { ...menu, parent_id: menu.parent_id } : { type: "menu", parent_id: parent, icon: "📄" }}
      >
        <Form.Item name="type" label="类型">
          <Radio.Group disabled={!!menu} options={[{ value: "menu", label: "菜单（对应一个页面）" }, { value: "dir", label: "目录（只用来分组）" }]} />
        </Form.Item>
        <Space align="start">
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input maxLength={32} />
          </Form.Item>
          <Form.Item name="code" label="编码" rules={[{ required: true, pattern: /^[a-z][a-z0-9_]{1,31}$/, message: "小写字母开头，小写字母、数字、下划线" }]}>
            <Input disabled={!!menu} />
          </Form.Item>
          <Form.Item name="icon" label="图标">
            <Input maxLength={4} style={{ width: 80 }} />
          </Form.Item>
        </Space>
        <Form.Item name="parent_id" label="上级目录">
          <Select allowClear placeholder="（顶级）" options={dirs.filter((d) => d.id !== menu?.id).map((d) => ({ value: d.id, label: d.name }))} />
        </Form.Item>
        {type === "menu" && (
          <>
            <Form.Item name="path" label="路由" rules={[{ required: true, pattern: /^\/[\w\-/]*$/, message: "以 / 开头" }]}>
              <Input placeholder="/reports" />
            </Form.Item>
            <Form.Item name="is_public" valuePropName="checked">
              <Checkbox>公共菜单（所有能访问该应用的人都可见，无需角色绑定）</Checkbox>
            </Form.Item>
          </>
        )}
      </Form>
    </Modal>
  );
}

// ───────────────────────────────────────────── 角色

function RolesTab({ app }: { app: AppInfo }) {
  const can = useCan();
  const qc = useQueryClient();
  const { message } = App.useApp();
  const roles = useQuery({ queryKey: ["app", app.id, "roles"], queryFn: () => api.get<Role[]>(`/apps/${app.id}/roles`) });
  const [sel, setSel] = useState<string>();
  const [form, setForm] = useState<{ open: boolean; mode?: "new" | "edit" | "copy" }>({ open: false });
  const current = roles.data?.find((r) => r.id === sel) ?? roles.data?.[0];
  return (
    <div style={{ display: "grid", gridTemplateColumns: "280px minmax(0,1fr)", gap: 16, alignItems: "start" }}>
      <Card size="small" title={`角色 ${roles.data?.length ?? 0} 个`} extra={can("role:manage") && <Button size="small" type="primary" onClick={() => setForm({ open: true, mode: "new" })}>＋ 新建</Button>}>
        <List
          dataSource={roles.data ?? []}
          locale={{ emptyText: "该应用还没有角色" }}
          renderItem={(r) => (
            <List.Item
              onClick={() => setSel(r.id)}
              style={{ cursor: "pointer", padding: "8px 10px", borderRadius: 8, background: r.id === current?.id ? "#eef0ff" : undefined }}
            >
              <div>
                <Space>
                  <b style={{ color: r.id === current?.id ? "#4F46E5" : undefined }}>{r.name}</b>
                  {r.is_builtin && <Tag>内置</Tag>}
                </Space>
                <div className="uc-muted" style={{ fontSize: 12 }}>
                  {r.operation_count} 项操作 · {r.holder_count} 人
                </div>
              </div>
            </List.Item>
          )}
        />
      </Card>
      {current ? <RoleDetail app={app} roleId={current.id} onEdit={(mode) => setForm({ open: true, mode })} onDeleted={() => setSel(undefined)} /> : <Card><Empty description="请先在「操作目录」登记操作，再新建角色" /></Card>}
      {form.open && (
        <RoleForm
          app={app}
          mode={form.mode!}
          role={form.mode === "new" ? null : current ?? null}
          onClose={() => setForm({ open: false })}
          onDone={(id) => (qc.invalidateQueries({ queryKey: ["app", app.id] }), setSel(id), message.success("已保存"))}
        />
      )}
    </div>
  );
}

function RoleForm({ app, mode, role, onClose, onDone }: { app: AppInfo; mode: "new" | "edit" | "copy"; role: Role | null; onClose: () => void; onDone: (id: string) => void }) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  async function submit(v: { name: string; code: string; description?: string }) {
    try {
      if (mode === "edit" && role) {
        await api.patch(`/roles/${role.id}`, { name: v.name, description: v.description });
        onDone(role.id);
      } else {
        const r = await api.post<Role>(`/apps/${app.id}/roles`, { ...v, copy_from: mode === "copy" ? role?.id : undefined });
        onDone(r.id);
      }
      onClose();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open title={mode === "edit" ? `编辑角色 · ${role?.name}` : mode === "copy" ? `复制角色 · ${role?.name}` : "新建角色"} onCancel={onClose} onOk={() => form.submit()} okText="保存">
      <Form form={form} layout="vertical" onFinish={submit} initialValues={mode === "edit" ? role ?? {} : mode === "copy" ? { name: `${role?.name}（副本）`, description: role?.description } : {}}>
        <div className="uc-muted" style={{ marginBottom: 12 }}>
          所属应用：{app.name}（角色属于应用，只能包含该应用的操作与菜单）
        </div>
        <Form.Item name="name" label="角色名称" rules={[{ required: true }]}>
          <Input maxLength={32} />
        </Form.Item>
        <Form.Item name="code" label="角色编码" extra="全局唯一，各应用以编码识别角色，创建后不可修改" rules={mode === "edit" ? [] : [{ required: true, pattern: /^[A-Z][A-Z0-9_]{1,31}$/, message: "大写字母开头，大写字母、数字、下划线" }]}>
          <Input disabled={mode === "edit"} />
        </Form.Item>
        <Form.Item name="description" label="说明">
          <Input maxLength={200} />
        </Form.Item>
        {mode === "copy" && <Alert type="info" showIcon message="将复制操作与菜单，不复制授权。" />}
      </Form>
    </Modal>
  );
}

function RoleDetail({ app, roleId, onEdit, onDeleted }: { app: AppInfo; roleId: string; onEdit: (mode: "edit" | "copy") => void; onDeleted: () => void }) {
  const can = useCan();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const role = useQuery({ queryKey: ["role", roleId], queryFn: () => api.get<Role>(`/roles/${roleId}`) });
  const ops = useQuery({ queryKey: ["app", app.id, "ops"], queryFn: () => api.get<Operation[]>(`/apps/${app.id}/operations`) });
  const menus = useQuery({ queryKey: ["app", app.id, "menus"], queryFn: () => api.get<MenuItem[]>(`/apps/${app.id}/menus`) });
  const grants = useQuery({ queryKey: ["role", roleId, "grants"], queryFn: () => api.get<{ grants: GrantRow[]; positions: { id: string; name: string; grants: GrantRow[] }[] }>(`/roles/${roleId}/grants`) });
  const holders = useQuery({ queryKey: ["role", roleId, "holders"], queryFn: () => api.get<{ user: { id: string; name: string; account: string; status: string }; dept: string; sources: string[] }[]>(`/roles/${roleId}/holders`) });
  const grantActs = useGrantMutations();
  const [opIds, setOpIds] = useState<string[]>([]);
  const [menuIds, setMenuIds] = useState<string[]>([]);
  const [grantTo, setGrantTo] = useState(false);
  const r = role.data;
  useEffect(() => {
    if (r) {
      setOpIds(r.operation_ids ?? []);
      setMenuIds(r.menu_ids ?? []);
    }
  }, [r]);
  const editable = !!r && can("role:manage") && !r.is_builtin;
  const modules = useMemo(() => {
    const m = new Map<string, Operation[]>();
    for (const o of ops.data ?? []) m.set(o.module, [...(m.get(o.module) ?? []), o]);
    return [...m.entries()];
  }, [ops.data]);
  const save = useMutation({
    mutationFn: (kind: "operations" | "menus") => api.put(`/roles/${roleId}/${kind}`, { ids: kind === "operations" ? opIds : menuIds }),
    onSuccess: () => (qc.invalidateQueries({ queryKey: ["role", roleId] }), qc.invalidateQueries({ queryKey: ["app", app.id] }), message.success("已保存；拥有者的权限随之更新")),
    onError: (e) => message.error(errorText(e)),
  });
  if (!r) return <Card loading />;

  async function remove() {
    const pre = await api.del<{ grants: number; positions: string[]; holders: number }>(`/roles/${roleId}`, { dry_run: true });
    modal.confirm({
      title: `删除角色 · ${r!.name}`,
      content: `将撤销 ${pre.grants} 条授权${pre.positions.length ? `，并从岗位 ${pre.positions.join("、")} 的默认角色中移除` : ""}；目前 ${pre.holders} 人拥有该角色。`,
      okButtonProps: { danger: true },
      onOk: async () => (await api.del(`/roles/${roleId}`), qc.invalidateQueries({ queryKey: ["app", app.id] }), onDeleted(), message.success("已删除")),
    });
  }

  const menuRowsList = menuRows(menus.data ?? []);
  const descendants = (id: string): string[] => (menus.data ?? []).filter((m) => m.parent_id === id).flatMap((m) => [m.id, ...descendants(m.id)]);
  return (
    <Card>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
        <div>
          <Space>
            <span style={{ fontSize: 19, fontWeight: 650 }}>{r.name}</span>
            {r.is_builtin && <Tag>内置</Tag>}
          </Space>
          <div className="uc-muted">
            <span className="uc-mono">{r.code}</span> · {r.description}
          </div>
        </div>
        <div className="uc-grow" />
        {can("role:manage") && <Button onClick={() => onEdit("copy")}>复制</Button>}
        {editable && <Button onClick={() => onEdit("edit")}>编辑</Button>}
        {editable && <Button danger onClick={remove}>删除</Button>}
      </div>
      <Tabs
        style={{ marginTop: 8 }}
        items={[
          {
            key: "ops",
            label: `操作权限（${opIds.length}）`,
            children: (
              <>
                {modules.map(([mod, list]) => (
                  <div key={mod} style={{ marginBottom: 12 }}>
                    <Checkbox
                      disabled={!editable}
                      checked={list.every((o) => opIds.includes(o.id))}
                      indeterminate={list.some((o) => opIds.includes(o.id)) && !list.every((o) => opIds.includes(o.id))}
                      onChange={(e) => setOpIds(e.target.checked ? [...new Set([...opIds, ...list.map((o) => o.id)])] : opIds.filter((x) => !list.some((o) => o.id === x)))}
                    >
                      <b>{mod}</b>
                    </Checkbox>
                    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", paddingLeft: 24 }}>
                      {list.map((o) => (
                        <Checkbox key={o.id} disabled={!editable} checked={opIds.includes(o.id)} onChange={(e) => setOpIds(e.target.checked ? [...opIds, o.id] : opIds.filter((x) => x !== o.id))}>
                          {o.name} <span className="uc-mono uc-muted">{o.code}</span>
                        </Checkbox>
                      ))}
                    </div>
                  </div>
                ))}
                {!modules.length && <Empty description="该应用还没有登记操作" />}
                {editable && <Button type="primary" onClick={() => save.mutate("operations")} loading={save.isPending}>保存</Button>}
                {r.is_builtin && <div className="uc-muted">内置角色的操作不可修改</div>}
              </>
            ),
          },
          {
            key: "menus",
            label: `菜单权限（${menuIds.length}）`,
            children: (
              <>
                {menuRowsList.map((m) => {
                  const kids = descendants(m.id).filter((id) => (menus.data ?? []).find((x) => x.id === id)?.type === "menu");
                  const checked = m.is_public || menuIds.includes(m.id) || (m.type === "dir" && kids.some((k) => menuIds.includes(k) || (menus.data ?? []).find((x) => x.id === k)?.is_public));
                  return (
                    <div key={m.id} style={{ paddingLeft: m.depth * 22 }}>
                      <Checkbox
                        disabled={!editable || m.is_public}
                        checked={checked}
                        onChange={(e) => {
                          const ids = m.type === "dir" ? kids : [m.id];
                          setMenuIds(e.target.checked ? [...new Set([...menuIds, ...ids])] : menuIds.filter((x) => !ids.includes(x)));
                        }}
                      >
                        {m.icon} {m.type === "dir" ? <b>{m.name}</b> : m.name} {m.path && <span className="uc-mono uc-muted">{m.path}</span>} {m.type === "dir" && <Tag>目录</Tag>}
                        {m.is_public && <Tag color="green">公共</Tag>}
                      </Checkbox>
                    </div>
                  );
                })}
                {!menuRowsList.length && <Empty description="该应用还没有菜单" />}
                <div className="uc-muted" style={{ margin: "8px 0" }}>
                  公共菜单所有人可见，无需绑定；目录只在至少有一个可见子菜单时显示。
                </div>
                {editable && <Button type="primary" onClick={() => save.mutate("menus")} loading={save.isPending}>保存</Button>}
              </>
            ),
          },
          {
            key: "grants",
            label: `授权（${grants.data?.grants.length ?? 0}）`,
            children: (
              <Space direction="vertical" size={16} style={{ width: "100%" }}>
                <Card size="small" title="授予的用户和组织" extra={can("grant:manage") && <Button size="small" type="primary" onClick={() => setGrantTo(true)}>＋ 授权</Button>}>
                  <Table<GrantRow>
                    size="small"
                    rowKey="id"
                    pagination={false}
                    dataSource={grants.data?.grants ?? []}
                    columns={[
                      { title: "授权对象", render: (_, g) => `${g.subject.type === "user" ? "👤" : "🏢"} ${g.subject.name}` },
                      { title: "范围", render: (_, g) => (g.subject.type === "user" ? "本人" : g.include_sub ? "本部门及下级部门" : "仅本部门") },
                      { title: "覆盖人数", dataIndex: "covered" },
                      { title: "", align: "right", render: (_, g) => can("grant:manage") && <Button type="link" size="small" danger onClick={() => grantActs.revoke(g)}>撤销</Button> },
                    ]}
                  />
                </Card>
                <Card size="small" title="通过岗位授予" extra={<span className="uc-muted">以下岗位的默认角色包含「{r.name}」</span>}>
                  {grants.data?.positions.length ? (
                    grants.data.positions.map((p) => (
                      <div key={p.id} style={{ padding: "4px 0" }}>
                        <b>{p.name}</b>：{p.grants.map((g) => <Tag key={g.id}>{g.subject.name}</Tag>)}
                        {!p.grants.length && <span className="uc-muted">尚未授权</span>}
                      </div>
                    ))
                  ) : (
                    <span className="uc-muted">没有岗位关联该角色</span>
                  )}
                </Card>
              </Space>
            ),
          },
          {
            key: "holders",
            label: `拥有者（${holders.data?.length ?? 0}）`,
            children: (
              <Table
                size="small"
                rowKey={(h) => h.user.id}
                pagination={false}
                dataSource={holders.data ?? []}
                columns={[
                  { title: "用户", render: (_, h) => <Space><b>{h.user.name}</b><span className="uc-muted">{h.user.account}</span>{h.user.status === "disabled" && <StatusTag status="disabled" />}</Space> },
                  { title: "部门", dataIndex: "dept" },
                  { title: "来源", render: (_, h) => <span className="uc-muted">{h.sources.join("、")}</span> },
                ]}
              />
            ),
          },
        ]}
      />
      {grantTo && <GrantToModal kind="role" target={r} onClose={() => setGrantTo(false)} />}
    </Card>
  );
}

// ───────────────────────────────────────────── 数据编码

function DataTypesTab({ app }: { app: AppInfo }) {
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const list = useQuery({ queryKey: ["app", app.id, "dtypes"], queryFn: () => api.get<DataTypeInfo[]>(`/apps/${app.id}/data-types`) });
  const [open, setOpen] = useState(false);
  const [typeAcl, setTypeAcl] = useState<DataTypeInfo | null>(null);
  const [form] = Form.useForm();
  const refresh = () => qc.invalidateQueries({ queryKey: ["app", app.id] });
  async function create(v: { code: string; name: string; description?: string }) {
    try {
      await api.post(`/apps/${app.id}/data-types`, v);
      refresh();
      setOpen(false);
      message.success("已登记");
    } catch (e) {
      message.error(errorText(e));
    }
  }
  const code = list.data?.[0]?.code ?? "Agent";
  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card size="small" title="数据编码" extra={<Button size="small" type="primary" onClick={() => (form.resetFields(), setOpen(true))}>＋ 登记数据编码</Button>}>
        <Table<DataTypeInfo>
          size="small"
          rowKey="id"
          pagination={false}
          dataSource={list.data ?? []}
          columns={[
            { title: "数据编码", render: (_, d) => <b className="uc-mono">{d.code}</b> },
            { title: "名称", dataIndex: "name" },
            { title: "数据条数", dataIndex: "data_count" },
            {
              title: "全部数据授权",
              render: (_, d) => (
                <Button size="small" onClick={() => setTypeAcl(d)}>
                  查看 / 管理
                </Button>
              ),
            },
            { title: "说明", dataIndex: "description" },
            {
              title: "",
              align: "right",
              render: (_, d) => (
                <Button
                  type="link"
                  size="small"
                  danger
                  onClick={() =>
                    modal.confirm({
                      title: `删除数据编码 · ${d.code}`,
                      okButtonProps: { danger: true },
                      onOk: () => api.del(`/data-types/${d.id}`).then(() => (refresh(), message.success("已删除"))).catch((e) => message.error(errorText(e))),
                    })
                  }
                >
                  删除
                </Button>
              ),
            },
          ]}
        />
        <div className="uc-muted" style={{ marginTop: 8 }}>
          权限级别：只读 &lt; 读写 &lt; Owner（Owner 还能管理这条数据的授权）。「只读 / 读写」在数据上能做什么由应用自己解释。
          <br />
          全部数据授权：对该编码下<b>全部</b>数据的级别（如「全部智能体 · Owner」），与单条数据的授权取高；它不经角色表达，也不计入单条数据的 Owner。
        </div>
      </Card>
      <Card size="small" title="接口（应用调用）">
        <pre className="uc-code">{`// ① 创建数据后写入创建人为 Owner（数据对象不存在时自动登记）
POST /open/v1/data-permissions
{ "data_code": "${code}", "data_id": "xxx_7f3a21", "data_name": "示例数据",
  "permission": "OWNER", "subject_type": "USER", "subject_account": "zhang.ming" }

// ② 打开 / 编辑 / 删除数据前校验
GET /open/v1/data-permissions/check?data_code=${code}&data_id=xxx_7f3a21&user_id=<用户 uuid>
→ { "permission": "WRITE", "sources": [...] }

// ③ 列表页过滤
GET /open/v1/data-permissions/accessible?data_code=${code}&user_id=<用户 uuid>&min=READ

// ④ 数据删除后清理
DELETE /open/v1/data-permissions?data_code=${code}&data_id=xxx_7f3a21

// ⑤ 用户对该编码下全部数据的级别（数据编码级授权）—— 判断「能不能看 / 管全部」
GET /open/v1/data-permissions/type?data_code=${code}&user_id=<用户 uuid>
→ { "permission": "OWNER", "sources": [...] }

// 已有授权的数据，应用代表用户修改时须带 operator_id（且其为 Owner）`}</pre>
      </Card>
      <Modal open={open} title={`登记数据编码 · ${app.name}`} onCancel={() => setOpen(false)} onOk={() => form.submit()} okText="保存">
        <Form form={form} layout="vertical" onFinish={create}>
          <Form.Item name="code" label="数据编码" extra="大写字母开头，字母和数字，全局唯一，创建后不可改" rules={[{ required: true, pattern: /^[A-Z][A-Za-z0-9]{1,31}$/, message: "格式不正确" }]}>
            <Input placeholder="如 KnowledgeBase" />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如 知识库" />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input />
          </Form.Item>
        </Form>
      </Modal>
      {typeAcl && <TypeAclModal dt={typeAcl} onClose={() => setTypeAcl(null)} />}
    </Space>
  );
}

const LEVEL_OPTS = (["READ", "WRITE", "OWNER"] as const).map((l) => ({ value: l, label: LEVEL[l].label }));

interface TypeAclView {
  data_type: { id: string; code: string; name: string; app: string };
  my_level: Level;
  can_manage: boolean;
  acl: {
    id: string;
    permission: Level;
    subject: { type: "USER" | "DEPT"; id: string; name: string; account?: string; status?: string };
    include_sub: boolean;
    granted_at: string;
  }[];
}

/** 数据编码级授权：对该编码下全部数据的级别。属于数据权限领域，与角色无关。 */
function TypeAclModal({ dt, onClose }: { dt: DataTypeInfo; onClose: () => void }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const key = ["data-type-acl", dt.id];
  const q = useQuery({ queryKey: key, queryFn: () => api.get<TypeAclView>(`/data-types/${dt.id}/acl`) });
  const [level, setLevel] = useState<Level>("OWNER");
  const [value, setValue] = useState<SubjectValue>({ depts: [], users: [], includeSub: true });
  const act = async (fn: () => Promise<unknown>, ok: string) => {
    try {
      await fn();
      await qc.invalidateQueries({ queryKey: key });
      message.success(ok);
    } catch (e) {
      message.error(errorText(e));
    }
  };
  async function add() {
    const subjects = toSubjects(value);
    if (!subjects.length) return message.warning("请选择组织或用户");
    await act(() => api.post(`/data-types/${dt.id}/acl`, { level, subjects, include_sub: value.includeSub }), "已授权");
    setValue({ depts: [], users: [], includeSub: true });
  }
  const v = q.data;
  return (
    <Modal open title={`全部数据授权 · ${dt.name}（${dt.code}）`} onCancel={onClose} footer={null} width={720}>
      <div className="uc-muted" style={{ marginBottom: 12 }}>
        对「{dt.name}」下<b>全部</b>数据的级别，与单条数据的授权取高。由用户中心的权限管理员或该编码的 Owner 维护。
      </div>
      <Table
        size="small"
        rowKey="id"
        loading={q.isLoading}
        pagination={false}
        dataSource={v?.acl ?? []}
        locale={{ emptyText: "尚无全部数据授权" }}
        columns={[
          {
            title: "授权对象",
            render: (_, a) =>
              a.subject.type === "USER" ? (
                <Space>
                  <b>{a.subject.name}</b>
                  <span className="uc-muted">{a.subject.account}</span>
                  {a.subject.status === "disabled" && <Tag>已停用</Tag>}
                </Space>
              ) : (
                <b>🏢 {a.subject.name}</b>
              ),
          },
          {
            title: "权限",
            render: (_, a) =>
              v?.can_manage ? (
                <Select size="small" value={a.permission} style={{ width: 100 }} options={LEVEL_OPTS} onChange={(lv) => act(() => api.patch(`/data-type-acl/${a.id}`, { level: lv }), "已调整")} />
              ) : (
                <LevelTag level={a.permission} />
              ),
          },
          { title: "范围", render: (_, a) => (a.subject.type === "USER" ? "本人" : a.include_sub ? "本部门及下级部门" : "仅本部门") },
          { title: "时间", render: (_, a) => <span className="uc-muted">{fmtTime(a.granted_at)}</span> },
          {
            title: "",
            align: "right",
            render: (_, a) =>
              v?.can_manage && (
                <Button type="link" size="small" danger onClick={() => act(() => api.del(`/data-type-acl/${a.id}`), "已移除")}>
                  移除
                </Button>
              ),
          },
        ]}
      />
      {v?.can_manage && (
        <Card size="small" title="添加" style={{ marginTop: 12 }}>
          <Space direction="vertical" style={{ width: "100%" }}>
            <Radio.Group value={level} onChange={(e) => setLevel(e.target.value)} options={LEVEL_OPTS} />
            <SubjectPicker value={value} onChange={setValue} />
            <Button type="primary" onClick={add}>
              授权
            </Button>
          </Space>
        </Card>
      )}
    </Modal>
  );
}
