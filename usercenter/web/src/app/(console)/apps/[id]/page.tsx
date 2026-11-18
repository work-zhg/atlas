"use client";

/**
 * 应用详情：接入配置 / 操作 / 菜单 / 数据编码。
 * ★ 角色挪到了「权限 › 角色」集中管理（「操作」页签有入口，筛好本应用）。
 * ★ 内置应用（用户中心）只有操作、菜单，只读。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Checkbox, Descriptions, Dropdown, Form, Input, Modal, Radio, Select, Space, Table, Tabs, Tag, Typography } from "antd";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { AppIcon, DeptSelect, DetailHead, LevelTag, showOnce, SubjectPicker, toSubjects, UserSelect, type SubjectValue } from "@/components/common";
import { Icon } from "@/components/Icon";
import { menuRows } from "@/components/roles";
import { api, errorText } from "@/lib/api";
import { fmtTime, HOME_URL_RULE, LEVEL } from "@/lib/format";
import { useCan } from "@/lib/session";
import type { AppInfo, DataTypeInfo, Level, MenuItem, MenuNode, Operation } from "@/lib/types";

export default function AppDetailPage() {
  const { id } = useParams<{ id: string }>();
  const can = useCan();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const app = useQuery({ queryKey: ["app", id], queryFn: () => api.get<AppInfo>(`/apps/${id}`) });
  const a = app.data;
  if (!a) return <Card loading />;

  const tabs = [
    // 凭据属于平台（app:manage）；可访问范围属于本租户（tenant_app:manage）
    !a.is_builtin && (can("app:manage") || can("tenant_app:manage")) && { key: "conf", label: "接入配置", children: <ConfTab app={a} /> },
    { key: "ops", label: `操作 ${a.operations}`, children: <OpsTab app={a} /> },
    { key: "menus", label: `菜单 ${a.menus}`, children: <MenusTab app={a} /> },
    !a.is_builtin && can("app:manage") && { key: "dtypes", label: `数据编码 ${a.data_types}`, children: <DataTypesTab app={a} /> },
  ].filter(Boolean) as { key: string; label: string; children: React.ReactNode }[];

  function toggle() {
    if (!a) return;
    const disabling = a.status === "active";
    modal.confirm({
      title: disabling ? `停用应用 · ${a.name}` : `启用应用 · ${a.name}`,
      content: disabling ? "停用后该应用不能再获取接口令牌，已签发的令牌立即失效；角色、菜单与授权全部保留。" : "启用后应用可重新获取接口令牌。",
      okText: disabling ? "停用" : "启用",
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
      <DetailHead
        back="/apps"
        icon={<AppIcon app={a} size={44} />}
        title={a.name}
        tags={a.is_builtin ? <Tag>内置</Tag> : a.status === "active" ? <Tag color="success">已启用</Tag> : <Tag>已停用</Tag>}
        sub={a.is_builtin ? "本系统 · 操作与菜单随版本发布（只读）" : `${a.description ?? ""}${a.description ? " · " : ""}接入于 ${fmtTime(a.created_at)}`}
        extra={
          !a.is_builtin &&
          can("app:manage") && (
            <Button danger={a.status === "active"} type={a.status === "active" ? "default" : "primary"} onClick={toggle}>
              {a.status === "active" ? "停用" : "启用"}
            </Button>
          )
        }
      />
      {a.status === "disabled" && <Alert type="warning" showIcon style={{ margin: "8px 0 4px" }} message="应用已停用：开放接口拒绝该应用的调用。" />}
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
  const refresh = () => (qc.invalidateQueries({ queryKey: ["app", app.id] }), qc.invalidateQueries({ queryKey: ["apps"] }));
  const rotate = () =>
    modal.confirm({
      title: `重新生成 Secret · ${app.name}`,
      content: "旧 Secret 及其签发的接口令牌立即失效，应用在更新配置前无法调用开放接口。",
      okText: "重新生成",
      okButtonProps: { danger: true },
      onOk: async () => {
        const r = await api.post<{ app: AppInfo; app_secret: string }>(`/apps/${app.id}/rotate-secret`);
        refresh();
        showOnce(modal, "新的 App Secret", [["App Key", r.app.app_key ?? ""], ["App Secret", r.app_secret]], "App Secret 只显示这一次，只能保存在应用后端。");
      },
    });
  async function patch(field: "code" | "home_url", value: string, ok: string) {
    try {
      await api.patch(`/apps/${app.id}`, { [field]: value.trim() || null, version: app.version });
      refresh();
      message.success(ok);
    } catch (e) {
      message.error(errorText(e));
    }
  }
  async function saveScope() {
    try {
      const r = await api.put<{ accessible_users: number }>(`/apps/${app.id}/scope`, { all, dept_ids: all ? [] : depts });
      refresh();
      message.success(`已保存，当前 ${r.accessible_users} 人可访问`);
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)", gap: 16, alignItems: "start" }}>
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        {can("app:manage") && (
          <Card size="small" title="应用凭据" extra={<Tag color="blue">服务端调用开放接口</Tag>}>
            <Descriptions column={1} size="small" styles={{ label: { width: 96 } }}>
              <Descriptions.Item label="应用编码">
                <Typography.Text code editable={{ tooltip: "内网服务调用开放接口时，请求头 X-Caller-App 写它", onChange: (v) => void patch("code", v, "已保存应用编码") }}>
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
                  <Button size="small" icon={<Icon name="refresh" size={14} />} onClick={rotate}>
                    重新生成
                  </Button>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="入口地址">
                <Typography.Text
                  code
                  editable={{
                    tooltip: "「应用切换」「我的应用」点开去这里",
                    onChange: (v) => {
                      if (v.trim() && !HOME_URL_RULE.pattern.test(v.trim())) return void message.error(HOME_URL_RULE.message);
                      void patch("home_url", v, v.trim() ? "已保存入口地址" : "已清空入口地址");
                    },
                  }}
                >
                  {app.home_url ?? ""}
                </Typography.Text>
                {!app.home_url && <span className="uc-sub">未登记：不出现在应用切换里</span>}
              </Descriptions.Item>
            </Descriptions>
            <div className="uc-sub">Secret 只在生成时显示一次，只能保存在应用后端，不能下发到浏览器。</div>
          </Card>
        )}
        <Card size="small" title="开放接口">
          <Descriptions column={1} size="small" styles={{ label: { width: 96 } }}>
            <Descriptions.Item label="Base URL">
              <span className="uc-mono">/open/v1</span>
            </Descriptions.Item>
            <Descriptions.Item label="获取令牌">
              <span className="uc-mono">POST /auth/token</span>
              <span className="uc-muted">（带 tenant_code：令牌绑定到该租户）</span>
            </Descriptions.Item>
            <Descriptions.Item label="主要接口">用户与部门查询、授权快照（可否访问 / 角色 / 操作码 / 菜单）、实时校验、数据权限</Descriptions.Item>
          </Descriptions>
        </Card>
      </Space>
      <Card
        size="small"
        title={
          <span>
            可访问范围 <span className="uc-sub">本租户</span>
          </span>
        }
        extra={<Tag color={(app.accessible_users ?? 0) > 0 ? "success" : "error"}>当前 {app.accessible_users ?? 0} 人可访问</Tag>}
      >
        <Space direction="vertical" size={10} style={{ width: "100%" }}>
          <button type="button" className="uc-radio-card" role="radio" aria-checked={all} onClick={() => setAll(true)}>
            <span className="dot" />
            <span>
              <b>全员</b>
              <div className="uc-sub">本租户所有未停用的人</div>
            </span>
          </button>
          <div className="uc-radio-card" role="radio" aria-checked={!all} onClick={() => setAll(false)}>
            <span className="dot" />
            <span className="uc-grow">
              <b>指定部门</b>
              <div className="uc-sub">含这些部门的下级部门</div>
              {!all && (
                <div style={{ marginTop: 10 }} onClick={(e) => e.stopPropagation()}>
                  <DeptSelect multiple value={depts} onChange={(v) => setDepts(v as string[])} />
                  {!depts.length && <div style={{ color: "var(--danger)", marginTop: 6, fontSize: 12.5 }}>还没选部门：没有人能访问</div>}
                </div>
              )}
            </span>
          </div>
          <div className="uc-row">
            <span className="uc-sub">访问不了的人在应用里会看到「没有访问权限」</span>
            <div className="uc-grow" />
            <Button type="primary" onClick={saveScope}>
              保存范围
            </Button>
          </div>
        </Space>
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
          <span className="uc-sub">该应用可被授权的操作{app.is_builtin ? " · 内置应用只读" : ""}</span>
          {can("role:view") && <Link href={`/perm/roles?app=${app.id}`}>用这些操作配角色：权限 › 角色 →</Link>}
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
            <span className="uc-muted">{app.is_builtin ? "内置菜单随版本发布" : "在「权限 › 角色 › 菜单权限」里绑定给角色"}</span>
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
        <div className="uc-pv" style={{ marginTop: 10 }}>
          <div className="uc-row" style={{ padding: "4px 10px 8px", borderBottom: "1px solid var(--line)", marginBottom: 6, fontWeight: 600 }}>
            <AppIcon app={app} size={20} />
            {app.name}
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
