"use client";

/**
 * 一个应用的目录：基本信息与凭据、操作码、菜单、数据编码、应用内置角色（所有企业共用）。
 * 产品自带的应用：目录随版本发布，这里只能查看（可以加内置角色）。企业自定义的角色不在这里，也看不到。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Checkbox, Dropdown, Empty, Form, Input, Modal, Popconfirm, Select, Space, Switch, Table, Tabs, Tag, Tooltip, Typography } from "antd";
import { useParams } from "next/navigation";
import { useMemo, useState, type ReactNode } from "react";

import { AppIcon, DetailHead, ErrorBox, OBSERVER_TIP, OneTimeSecret, SquareIcon, colorOf, errorText } from "@/components/common";
import { Icon } from "@/components/Icon";
import { UC, sys } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { useMe } from "@/lib/session";
import type { AppInfo, DataTypeInfo, MenuInfo, OpInfo, RoleInfo } from "@/lib/types";

const uc = sys(UC);
const SOURCE: Record<string, string> = { builtin: "产品自带", manual: "手动" };

function useAct() {
  const { message } = App.useApp();
  const qc = useQueryClient();
  return async (fn: () => Promise<unknown>, ok: string, keys: unknown[][]) => {
    try {
      await fn();
      if (ok) message.success(ok);
      await Promise.all(keys.map((k) => qc.invalidateQueries({ queryKey: k })));
      return true;
    } catch (e) {
      message.error(errorText(e));
      return false;
    }
  };
}

export default function AppPage() {
  const { id } = useParams<{ id: string }>();
  const { data: me } = useMe();
  const admin = me?.role === "ADMIN";
  const act = useAct();
  const { modal } = App.useApp();
  const q = useQuery({ queryKey: ["app", id], queryFn: () => uc.get<AppInfo>(`/apps/${id}`) });
  const [edit, setEdit] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const app = q.data;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!app) return null;
  const editable = admin && !app.is_builtin;
  const keys = [["app", app.id], ["apps"]];

  async function rotate() {
    try {
      const out = await uc.post<{ app_secret: string }>(`/apps/${app!.id}/rotate-secret`);
      setSecret(out.app_secret);
    } catch (e) {
      await act(() => Promise.reject(e), "", []);
    }
  }

  return (
    <>
      <DetailHead
        back="/apps"
        icon={<AppIcon app={app} size={40} />}
        title={app.name}
        tags={
          <>
            {app.is_builtin && <Tag>产品自带</Tag>}
            {app.status === "disabled" && <Tag color="error">已停用</Tag>}
          </>
        }
        sub={
          <>
            应用编码 <span className="ops-mono">{app.code ?? "—"}</span> · {app.is_builtin ? "所有企业都能用" : `${app.tenants ?? 0} 个企业开通`}
          </>
        }
        extra={
          !app.is_builtin && (
            <>
              <Tooltip title={admin ? undefined : OBSERVER_TIP}>
                <Button icon={<Icon name="pencil" size={14} />} disabled={!editable} onClick={() => setEdit(true)}>
                  编辑
                </Button>
              </Tooltip>
              <Dropdown
                trigger={["click"]}
                placement="bottomRight"
                menu={{
                  items: [
                    { key: "rotate", icon: <Icon name="refresh" size={14} />, label: "轮换 App Secret", disabled: !editable },
                    app.status === "active"
                      ? { key: "disable", icon: <Icon name="ban" size={14} />, label: "停用", danger: true, disabled: !editable }
                      : { key: "enable", icon: <Icon name="power" size={14} />, label: "启用", disabled: !editable },
                  ],
                  onClick: ({ key }) => {
                    if (key === "rotate")
                      modal.confirm({ title: "轮换 App Secret", content: "旧的 App Secret 立即失效，用它的外部应用要马上换上新的。", okText: "轮换", onOk: rotate });
                    if (key === "disable")
                      modal.confirm({
                        title: `停用 ${app.name}`,
                        content: "停用后所有企业都用不了这个应用，它调用户中心也会被拒。企业里的授权都留着，启用后恢复。",
                        okText: "停用",
                        okButtonProps: { danger: true },
                        onOk: () => act(() => uc.post(`/apps/${app.id}/disable`), "已停用", keys),
                      });
                    if (key === "enable") void act(() => uc.post(`/apps/${app.id}/enable`), "已启用", keys);
                  },
                }}
              >
                <Button aria-label="更多" icon={<Icon name="more" />} />
              </Dropdown>
            </>
          )
        }
      />
      {secret && (
        <div style={{ margin: "12px 0" }}>
          <OneTimeSecret label="新的 App Secret（旧的已失效）" value={secret} />
        </div>
      )}
      <Tabs
        style={{ marginTop: 8 }}
        items={[
          { key: "info", label: "基本信息", children: <Info app={app} /> },
          { key: "ops", label: `操作码 ${app.operations ?? 0}`, children: <Operations appId={id} editable={editable} builtin={app.is_builtin} /> },
          { key: "menus", label: `菜单 ${app.menus ?? 0}`, children: <Menus appId={id} editable={editable} builtin={app.is_builtin} /> },
          { key: "data", label: `数据编码 ${app.data_types ?? 0}`, children: <DataTypes appId={id} editable={editable} builtin={app.is_builtin} /> },
          { key: "roles", label: `内置角色 ${app.roles ?? 0}`, children: <Roles appId={id} admin={admin} /> },
        ]}
      />
      {edit && (
        <FormModal
          title={`编辑 ${app.name}`}
          initial={app}
          fields={[
            { name: "name", label: "名称", required: true },
            { name: "code", label: "应用编码" },
            { name: "icon", label: "图标", extra: "一个 emoji" },
            { name: "home_url", label: "入口地址", extra: "站内路径（/atlas）或完整地址；不填 = 不在应用切换里出现" },
            { name: "description", label: "说明", textarea: true },
          ]}
          onClose={() => setEdit(false)}
          onSubmit={(v) => act(() => uc.patch(`/apps/${app.id}`, { ...v, version: app.version }), "已保存", keys)}
        />
      )}
    </>
  );
}

// ───────────────────────────── 基本信息

function Info({ app }: { app: AppInfo }) {
  return (
    <>
      {app.is_builtin && (
        <Alert type="info" showIcon style={{ marginBottom: 14 }} message="产品自带的应用：所有企业都能用；操作码、菜单、数据编码随版本发布，这里只能查看" />
      )}
      <Card>
        <dl className="ops-kv">
          <dt>名称</dt>
          <dd>{app.name}</dd>
          <dt>应用编码</dt>
          <dd>
            <span className="ops-mono">{app.code ?? "—"}</span>
            <span className="ops-sub">内网调用表明身份用（X-Caller-App）</span>
          </dd>
          <dt>状态</dt>
          <dd>
            {app.status === "active" ? <Tag color="success">正常</Tag> : <Tag color="error">已停用</Tag>}
            {app.status !== "active" && <span className="ops-sub">所有企业都用不了，它调用户中心也会被拒</span>}
          </dd>
          <dt>入口地址</dt>
          <dd>
            <span className="ops-mono">{app.home_url ?? "—"}</span>
            <span className="ops-sub">出现在各应用顶栏的「应用切换」里</span>
          </dd>
          <dt>App Key</dt>
          <dd>{app.app_key ? <Typography.Text copyable className="ops-mono">{app.app_key}</Typography.Text> : "—"}</dd>
          <dt>App Secret</dt>
          <dd>
            <span className="ops-mono">{app.secret_tail ? `••••••••${app.secret_tail}` : "—"}</span>
            <span className="ops-sub">经网关接入的外部应用用；只在接入、轮换时显示一次</span>
          </dd>
          <dt>说明</dt>
          <dd>{app.description || "—"}</dd>
          <dt>接入时间</dt>
          <dd>{dateTime(app.created_at)}</dd>
        </dl>
      </Card>
    </>
  );
}

// ───────────────────────────── 通用的表单弹窗

interface Field {
  name: string;
  label: string;
  required?: boolean;
  textarea?: boolean;
  disabled?: boolean;
  extra?: string;
  render?: () => ReactNode;
}

function FormModal({
  title,
  initial,
  fields,
  onClose,
  onSubmit,
}: {
  title: string;
  initial?: object;
  fields: Field[];
  onClose: () => void;
  onSubmit: (v: Record<string, unknown>) => Promise<boolean>;
}) {
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);
  return (
    <Modal open title={title} okText="保存" onCancel={onClose} onOk={() => form.submit()} confirmLoading={busy}>
      <Form
        form={form}
        layout="vertical"
        requiredMark={false}
        initialValues={initial}
        onFinish={async (v) => {
          setBusy(true);
          const ok = await onSubmit(v);
          setBusy(false);
          if (ok) onClose();
        }}
      >
        {fields.map((f) => (
          <Form.Item key={f.name} name={f.name} label={f.label} extra={f.extra} rules={f.required ? [{ required: true, message: `请输入${f.label}` }] : []}>
            {f.render ? f.render() : f.textarea ? <Input.TextArea rows={2} /> : <Input disabled={f.disabled} />}
          </Form.Item>
        ))}
      </Form>
    </Modal>
  );
}

function Toolbar({ hint, children }: { hint: ReactNode; children?: ReactNode }) {
  return (
    <div className="ops-toolbar">
      <span className="ops-sub ops-grow">{hint}</span>
      {children}
    </div>
  );
}

const readonlyTip = (builtin: boolean) => (builtin ? "产品自带的应用随版本发布，这里只能查看" : OBSERVER_TIP);

// ───────────────────────────── 操作码

function Operations({ appId, editable, builtin }: { appId: string; editable: boolean; builtin: boolean }) {
  const act = useAct();
  const { modal } = App.useApp();
  const key = ["app-ops", appId];
  const q = useQuery({ queryKey: key, queryFn: () => uc.get<OpInfo[]>(`/apps/${appId}/operations`) });
  const [edit, setEdit] = useState<OpInfo | "new" | null>(null);
  const [kw, setKw] = useState("");
  // 按模块排好，模块那一格合并（同模块的放一起）
  const rows = useMemo(() => {
    const k = kw.trim();
    const list = (q.data ?? []).filter((o) => !k || o.code.includes(k) || o.name.includes(k) || o.module.includes(k));
    const mods = [...new Set(list.map((o) => o.module))];
    return mods.flatMap((m) => list.filter((o) => o.module === m).map((o, i, arr) => ({ ...o, span: i === 0 ? arr.length : 0 })));
  }, [q.data, kw]);

  async function remove(op: OpInfo) {
    try {
      const impact = await uc.del<{ roles: string[] }>(`/operations/${op.id}`, { dry_run: true });
      modal.confirm({
        title: `删除操作码 ${op.code}`,
        content: impact.roles.length
          ? `内置角色「${impact.roles.join("、")}」里的这个操作码会一起去掉；各企业自定义角色里的也会去掉。`
          : "各企业自定义角色里的这个操作码会一起去掉。",
        okText: "删除",
        okButtonProps: { danger: true },
        onOk: () => act(() => uc.del(`/operations/${op.id}`), "已删除", [key, ["app", appId]]),
      });
    } catch (e) {
      await act(() => Promise.reject(e), "", []);
    }
  }

  return (
    <>
      <Toolbar hint="操作码：应用代码里按它判断能不能做某件事；角色勾的就是它">
        <Input allowClear prefix={<Icon name="search" size={14} />} placeholder="搜索编码或名称" style={{ width: 240 }} value={kw} onChange={(e) => setKw(e.target.value)} />
        <Tooltip title={editable ? undefined : readonlyTip(builtin)}>
          <Button icon={<Icon name="plus" />} disabled={!editable} onClick={() => setEdit("new")}>
            新增操作码
          </Button>
        </Tooltip>
      </Toolbar>
      <ErrorBox error={q.error} />
      <Table
        rowKey="id"
        loading={q.isLoading}
        dataSource={rows}
        pagination={false}
        columns={[
          { title: "模块", dataIndex: "module", onCell: (o) => ({ rowSpan: o.span }), render: (v: string) => <b>{v}</b> },
          { title: "编码", dataIndex: "code", render: (v: string) => <span className="ops-mono">{v}</span> },
          { title: "名称", dataIndex: "name" },
          { title: "来源", dataIndex: "source", render: (v: string) => <Tag color={v === "builtin" ? "processing" : undefined}>{SOURCE[v] ?? v}</Tag> },
          {
            title: "所在的内置角色",
            dataIndex: "roles",
            render: (v: string[] = []) => (v.length ? v.map((r) => <Tag key={r}>{r}</Tag>) : <span className="ops-muted">—</span>),
          },
          {
            title: "",
            width: 120,
            align: "right",
            render: (_, op) =>
              editable && (
                <Space size={0}>
                  <Button type="link" size="small" onClick={() => setEdit(op)}>
                    编辑
                  </Button>
                  <Button type="link" size="small" danger onClick={() => void remove(op)}>
                    删除
                  </Button>
                </Space>
              ),
          },
        ]}
      />
      {edit && (
        <FormModal
          title={edit === "new" ? "新增操作码" : `编辑 ${edit.code}`}
          initial={edit === "new" ? {} : edit}
          fields={[
            { name: "module", label: "模块", required: true },
            {
              name: "code",
              label: "编码",
              required: true,
              disabled: edit !== "new",
              extra: "资源:动作（小写字母、数字、下划线），要和应用代码里判断的一致，建好后不能改",
            },
            { name: "name", label: "名称", required: true },
          ]}
          onClose={() => setEdit(null)}
          onSubmit={(v) =>
            edit === "new"
              ? act(() => uc.post(`/apps/${appId}/operations`, v), "已新增", [key, ["app", appId]])
              : act(() => uc.patch(`/operations/${edit.id}`, { module: v.module, name: v.name }), "已保存", [key])
          }
        />
      )}
    </>
  );
}

// ───────────────────────────── 菜单

type MenuRow = MenuInfo & { children?: MenuRow[] };

function Menus({ appId, editable, builtin }: { appId: string; editable: boolean; builtin: boolean }) {
  const act = useAct();
  const { modal } = App.useApp();
  const key = ["app-menus", appId];
  const q = useQuery({ queryKey: key, queryFn: () => uc.get<MenuInfo[]>(`/apps/${appId}/menus`) });
  const [edit, setEdit] = useState<MenuInfo | "new" | null>(null);
  const menus = q.data ?? [];
  const dirs = menus.filter((m) => m.type === "dir");
  // 树：目录 → 菜单（顺序按接口给的 sort）
  const tree = useMemo<MenuRow[]>(() => {
    const kids = (p: string | null): MenuRow[] =>
      menus.filter((m) => m.parent_id === p).map((m) => {
        const c = kids(m.id);
        return c.length ? { ...m, children: c } : m;
      });
    return kids(null);
  }, [menus]);

  async function remove(m: MenuInfo) {
    try {
      const impact = await uc.del<{ roles: string[] }>(`/menus/${m.id}`, { dry_run: true });
      modal.confirm({
        title: `删除菜单 ${m.name}`,
        content: impact.roles.length ? `内置角色「${impact.roles.join("、")}」上的这个菜单会一起去掉。` : "确定删除？",
        okText: "删除",
        okButtonProps: { danger: true },
        onOk: () => act(() => uc.del(`/menus/${m.id}`), "已删除", [key, ["app", appId]]),
      });
    } catch (e) {
      await act(() => Promise.reject(e), "", []);
    }
  }

  return (
    <>
      <Toolbar hint="菜单：应用前端按它显示导航；公共菜单能访问这个应用的人都看得到，其余按角色">
        <Tooltip title={editable ? undefined : readonlyTip(builtin)}>
          <Button icon={<Icon name="plus" />} disabled={!editable} onClick={() => setEdit("new")}>
            新增菜单
          </Button>
        </Tooltip>
      </Toolbar>
      <ErrorBox error={q.error} />
      {tree.length > 0 && (
        <Table<MenuRow>
          rowKey="id"
          loading={q.isLoading}
          dataSource={tree}
          pagination={false}
          expandable={{ defaultExpandAllRows: true, indentSize: 22 }}
          columns={[
            {
              title: "名称",
              render: (_, m) => (
                <span className="ops-row" style={{ display: "inline-flex", gap: 6 }}>
                  {m.icon ? <span>{m.icon}</span> : <Icon name={m.type === "dir" ? "folder" : "file"} size={14} style={{ color: "var(--fg-3)" }} />}
                  <span style={{ fontWeight: m.type === "dir" ? 600 : 400 }}>{m.name}</span>
                </span>
              ),
            },
            { title: "编码", dataIndex: "code", render: (v: string) => <span className="ops-mono">{v}</span> },
            { title: "路径", dataIndex: "path", render: (v: string | null) => <span className="ops-mono ops-muted">{v ?? "—"}</span> },
            { title: "可见", dataIndex: "is_public", render: (v: boolean, m) => (m.type === "dir" ? null : v ? <Tag color="success">公共</Tag> : <span className="ops-muted">按角色</span>) },
            { title: "角色数", dataIndex: "role_count" },
            { title: "来源", dataIndex: "source", render: (v: string) => <Tag color={v === "builtin" ? "processing" : undefined}>{SOURCE[v] ?? v}</Tag> },
            {
              title: "",
              width: 170,
              align: "right",
              render: (_, m) =>
                editable && (
                  <Space size={0}>
                    <Button type="link" size="small" title="上移" icon={<Icon name="up" size={14} />} onClick={() => void act(() => uc.post(`/menus/${m.id}/move-up`), "已上移", [key])} />
                    <Button type="link" size="small" onClick={() => setEdit(m)}>
                      编辑
                    </Button>
                    <Button type="link" size="small" danger onClick={() => void remove(m)}>
                      删除
                    </Button>
                  </Space>
                ),
            },
          ]}
        />
      )}
      {!q.isLoading && !tree.length && <Card><Empty description="还没有菜单" /></Card>}
      {edit && (
        <FormModal
          title={edit === "new" ? "新增菜单" : `编辑 ${edit.name}`}
          initial={edit === "new" ? { type: "menu", is_public: false } : edit}
          fields={[
            {
              name: "type",
              label: "类型",
              render: () => (
                <Select
                  disabled={edit !== "new"}
                  options={[
                    { value: "menu", label: "菜单" },
                    { value: "dir", label: "目录" },
                  ]}
                />
              ),
            },
            { name: "code", label: "编码", required: true, disabled: edit !== "new", extra: "要和应用前端一致" },
            { name: "name", label: "名称", required: true },
            { name: "icon", label: "图标" },
            { name: "path", label: "路径（菜单才有）", extra: "以 / 开头，要和应用前端的路由一致" },
            { name: "parent_id", label: "上级目录", render: () => <Select allowClear options={dirs.map((d) => ({ value: d.id, label: d.name }))} /> },
            { name: "is_public", label: "公共菜单（能访问这个应用的人都看得到）", render: () => <Switch /> },
          ]}
          onClose={() => setEdit(null)}
          onSubmit={(v) => {
            const body = { ...v, parent_id: v.parent_id ?? null };
            if (v.type === "dir") {
              delete (body as Record<string, unknown>).path;
              delete (body as Record<string, unknown>).is_public;
            }
            return edit === "new"
              ? act(() => uc.post(`/apps/${appId}/menus`, body), "已新增", [key, ["app", appId]])
              : act(
                  () => {
                    const { type: _t, code: _c, ...rest } = body as Record<string, unknown>;
                    return uc.patch(`/menus/${edit.id}`, rest);
                  },
                  "已保存",
                  [key],
                );
          }}
        />
      )}
    </>
  );
}

// ───────────────────────────── 数据编码

function DataTypes({ appId, editable, builtin }: { appId: string; editable: boolean; builtin: boolean }) {
  const act = useAct();
  const key = ["app-data-types", appId];
  const q = useQuery({ queryKey: key, queryFn: () => uc.get<DataTypeInfo[]>(`/apps/${appId}/data-types`) });
  const [edit, setEdit] = useState<DataTypeInfo | "new" | null>(null);
  return (
    <>
      <Toolbar hint="数据编码：按「哪条数据」授权的类别（如某个智能体、某个知识库），由企业管理员在用户中心授权">
        <Tooltip title={editable ? undefined : readonlyTip(builtin)}>
          <Button icon={<Icon name="plus" />} disabled={!editable} onClick={() => setEdit("new")}>
            新增数据编码
          </Button>
        </Tooltip>
      </Toolbar>
      <ErrorBox error={q.error} />
      <Table<DataTypeInfo>
        rowKey="id"
        loading={q.isLoading}
        dataSource={q.data ?? []}
        pagination={false}
        locale={{ emptyText: "还没有数据编码" }}
        columns={[
          { title: "编码", dataIndex: "code", render: (v: string) => <span className="ops-mono">{v}</span> },
          { title: "名称", dataIndex: "name" },
          { title: "说明", dataIndex: "description", render: (v: string | null) => <span className="ops-muted">{v || "—"}</span> },
          { title: "各企业登记的数据", dataIndex: "data_count", render: (v: number | undefined) => `${v ?? 0} 条` },
          {
            title: "",
            width: 120,
            align: "right",
            render: (_, d) =>
              editable && (
                <Space size={0}>
                  <Button type="link" size="small" onClick={() => setEdit(d)}>
                    编辑
                  </Button>
                  <Popconfirm title="还有任何企业的数据在用这个编码就删不掉" okText="删除" onConfirm={() => act(() => uc.del(`/data-types/${d.id}`), "已删除", [key, ["app", appId]])}>
                    <Button type="link" size="small" danger>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              ),
          },
        ]}
      />
      {edit && (
        <FormModal
          title={edit === "new" ? "新增数据编码" : `编辑 ${edit.code}`}
          initial={edit === "new" ? {} : edit}
          fields={[
            { name: "code", label: "编码", required: true, disabled: edit !== "new", extra: "大写字母开头，字母和数字（如 AtlasAgent），要和应用代码一致" },
            { name: "name", label: "名称", required: true },
            { name: "description", label: "说明", textarea: true },
          ]}
          onClose={() => setEdit(null)}
          onSubmit={(v) =>
            edit === "new"
              ? act(() => uc.post(`/apps/${appId}/data-types`, v), "已新增", [key, ["app", appId]])
              : act(() => uc.patch(`/data-types/${edit.id}`, { name: v.name, description: v.description }), "已保存", [key])
          }
        />
      )}
    </>
  );
}

// ───────────────────────────── 内置角色：左边列表，右边勾操作码 / 菜单

function Roles({ appId, admin }: { appId: string; admin: boolean }) {
  const act = useAct();
  const { modal } = App.useApp();
  const key = ["app-roles", appId];
  const q = useQuery({ queryKey: key, queryFn: () => uc.get<RoleInfo[]>(`/apps/${appId}/roles`) });
  const [sel, setSel] = useState<string | null>(null);
  const [edit, setEdit] = useState<RoleInfo | "new" | null>(null);
  const roles = q.data ?? [];
  const role = roles.find((r) => r.id === sel) ?? roles[0];

  async function remove(r: RoleInfo) {
    try {
      const impact = await uc.del<{ grants: number; positions: number }>(`/roles/${r.id}`, { dry_run: true });
      modal.confirm({
        title: `删除内置角色 ${r.name}`,
        content: `各企业里授了这个角色的 ${impact.grants} 条授权、${impact.positions} 个岗位上的默认角色会一起删掉。`,
        okText: "删除",
        okButtonProps: { danger: true },
        onOk: async () => {
          if (await act(() => uc.del(`/roles/${r.id}`), "已删除", [key, ["app", appId]])) setSel(null);
        },
      });
    } catch (e) {
      await act(() => Promise.reject(e), "", []);
    }
  }

  return (
    <>
      <ErrorBox error={q.error} />
      <div className="ops-split">
        <div className="ops-panel">
          <div className="ops-panel-head">
            <b className="ops-grow">内置角色</b>
            <Tooltip title={admin ? "新增内置角色" : OBSERVER_TIP}>
              <Button type="text" size="small" disabled={!admin} icon={<Icon name="plus" />} onClick={() => setEdit("new")} />
            </Tooltip>
          </div>
          <div className="ops-panel-body">
            {roles.map((r) => (
              <button key={r.id} type="button" className="ops-li" aria-current={r.id === role?.id} onClick={() => setSel(r.id)}>
                <SquareIcon icon="badge" color={colorOf(r.code)} size={28} />
                <div className="ops-grow">
                  <b>{r.name}</b>
                  <div className="ops-sub ops-mono">{r.code}</div>
                </div>
                {r.shared && (
                  <Tag color="processing" title="开通应用时自动授给企业里的所有人">
                    全员
                  </Tag>
                )}
              </button>
            ))}
            {!q.isLoading && !roles.length && <Empty description="还没有内置角色" />}
          </div>
          <div style={{ padding: "8px 12px", borderTop: "1px solid var(--line)" }} className="ops-sub">
            内置角色所有企业共用，企业里改不了（可以复制成自己的再改）
          </div>
        </div>
        {role ? <RoleDetail key={role.id} appId={appId} role={role} editable={admin && !role.is_builtin} onEdit={() => setEdit(role)} onRemove={() => void remove(role)} /> : <Card><Empty description="选一个角色" /></Card>}
      </div>
      {edit && (
        <FormModal
          title={edit === "new" ? "新增内置角色" : `编辑 ${edit.code}`}
          initial={edit === "new" ? {} : edit}
          fields={[
            { name: "code", label: "编码", required: true, disabled: edit !== "new", extra: "大写字母开头，大写字母、数字、下划线（如 ATLAS_MEMBER）" },
            { name: "name", label: "名称", required: true },
            { name: "description", label: "说明", textarea: true },
          ]}
          onClose={() => setEdit(null)}
          onSubmit={(v) =>
            edit === "new"
              ? act(() => uc.post(`/apps/${appId}/roles`, v), "已新增", [key, ["app", appId]])
              : act(() => uc.patch(`/roles/${edit.id}`, { name: v.name, description: v.description }), "已保存", [key])
          }
        />
      )}
    </>
  );
}

function RoleDetail({ appId, role, editable, onEdit, onRemove }: { appId: string; role: RoleInfo; editable: boolean; onEdit: () => void; onRemove: () => void }) {
  const act = useAct();
  const detail = useQuery({ queryKey: ["role", role.id], queryFn: () => uc.get<RoleInfo>(`/roles/${role.id}`) });
  const ops = useQuery({ queryKey: ["app-ops", appId], queryFn: () => uc.get<OpInfo[]>(`/apps/${appId}/operations`) });
  const menus = useQuery({ queryKey: ["app-menus", appId], queryFn: () => uc.get<MenuInfo[]>(`/apps/${appId}/menus`) });
  const [opIds, setOpIds] = useState<string[] | null>(null);
  const [menuIds, setMenuIds] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const curOps = opIds ?? detail.data?.operation_ids ?? [];
  const curMenus = menuIds ?? detail.data?.menu_ids ?? [];
  const dirty = opIds !== null || menuIds !== null;
  const mods = [...new Set((ops.data ?? []).map((o) => o.module))];
  const pickable = (menus.data ?? []).filter((m) => m.type === "menu" && !m.is_public);

  async function save() {
    setBusy(true);
    const keys = [["app-roles", appId], ["role", role.id], ["app-ops", appId], ["app-menus", appId]];
    const ok =
      (await act(() => uc.put(`/roles/${role.id}/operations`, { ids: curOps }), "", keys)) &&
      (await act(() => uc.put(`/roles/${role.id}/menus`, { ids: curMenus }), "已保存", keys));
    setBusy(false);
    if (ok) {
      setOpIds(null);
      setMenuIds(null);
    }
  }

  return (
    <Card
      title={
        <div>
          <div className="ops-row">
            {role.name}
            {role.is_builtin && <Tag>产品自带</Tag>}
            {role.shared && <Tag color="processing">开通后授给全员</Tag>}
          </div>
          <div className="ops-sub" style={{ fontWeight: 400 }}>
            {role.description || "没有说明"}
          </div>
        </div>
      }
      extra={
        editable && (
          <Space size={0}>
            <Button type="link" size="small" onClick={onEdit}>
              编辑
            </Button>
            <Button type="link" size="small" danger onClick={onRemove}>
              删除
            </Button>
          </Space>
        )
      }
      styles={{ body: { paddingTop: 0 } }}
    >
      <ErrorBox error={detail.error ?? ops.error ?? menus.error} />
      <Tabs
        items={[
          {
            key: "ops",
            label: `操作码 ${curOps.length}`,
            children: mods.length ? (
              <Checkbox.Group value={curOps} onChange={(v) => setOpIds(v as string[])} disabled={!editable} style={{ display: "block" }}>
                {mods.map((m) => (
                  <div key={m} style={{ marginBottom: 12 }}>
                    <div className="ops-subhead" style={{ margin: "0 0 6px" }}>
                      {m}
                    </div>
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 6 }}>
                      {(ops.data ?? [])
                        .filter((o) => o.module === m)
                        .map((o) => (
                          <Checkbox key={o.id} value={o.id} className="ops-opt">
                            <div>{o.name}</div>
                            <div className="ops-sub ops-mono">{o.code}</div>
                          </Checkbox>
                        ))}
                    </div>
                  </div>
                ))}
              </Checkbox.Group>
            ) : (
              <Empty description="这个应用还没有操作码" />
            ),
          },
          {
            key: "menus",
            label: `菜单 ${curMenus.length}`,
            children: pickable.length ? (
              <Checkbox.Group value={curMenus} onChange={(v) => setMenuIds(v as string[])} disabled={!editable} style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 6 }}>
                {pickable.map((m) => (
                  <Checkbox key={m.id} value={m.id} className="ops-opt">
                    <div>
                      {m.icon ?? ""} {m.name}
                    </div>
                    <div className="ops-sub ops-mono">{m.path}</div>
                  </Checkbox>
                ))}
              </Checkbox.Group>
            ) : (
              <Empty description="没有按角色控制的菜单（公共菜单人人可见，不用勾）" />
            ),
          },
        ]}
      />
      {editable ? (
        <div className="ops-row" style={{ borderTop: "1px solid var(--line)", paddingTop: 12, marginTop: 4 }}>
          <span className="ops-sub ops-grow">内置角色改了，所有企业里的这个角色都跟着变（企业自建的角色不受影响）</span>
          <Button
            disabled={!dirty}
            onClick={() => {
              setOpIds(null);
              setMenuIds(null);
            }}
          >
            还原
          </Button>
          <Button type="primary" disabled={!dirty} loading={busy} onClick={() => void save()}>
            保存
          </Button>
        </div>
      ) : (
        <div className="ops-sub" style={{ borderTop: "1px solid var(--line)", paddingTop: 12, marginTop: 4 }}>
          {role.is_builtin ? "产品自带的角色随版本发布，只能查看" : OBSERVER_TIP}
        </div>
      )}
    </Card>
  );
}
