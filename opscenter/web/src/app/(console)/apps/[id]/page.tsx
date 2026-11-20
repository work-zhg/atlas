"use client";

/**
 * 一个应用的目录：基本信息与凭据、操作码、菜单、数据编码、应用内置角色（所有租户共用）。
 * 都由运营人员录入、调整；应用代码里只按编码判断，不登记。租户自定义的角色不在这里，也看不到。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { useParams } from "next/navigation";
import { useState, type ReactNode } from "react";

import { ErrorBox, OneTimeSecret, PageHead, errorText } from "@/components/common";
import { UC, sys } from "@/lib/api";
import { useMe } from "@/lib/session";
import type { AppInfo, DataTypeInfo, MenuInfo, OpInfo, RoleInfo } from "@/lib/types";

const uc = sys(UC);

function useAct() {
  const { message } = App.useApp();
  const qc = useQueryClient();
  return async (fn: () => Promise<unknown>, ok: string, keys: unknown[][]) => {
    try {
      await fn();
      message.success(ok);
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
  const q = useQuery({ queryKey: ["app", id], queryFn: () => uc.get<AppInfo>(`/apps/${id}`) });
  const app = q.data;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!app) return null;
  const editable = me?.role === "ADMIN" && !app.is_builtin;
  return (
    <>
      <PageHead
        title={`${app.icon ?? ""} ${app.name}`}
        sub={app.is_builtin ? "产品自带的应用：目录随版本发布，这里只能查看" : "应用目录：所有租户共用"}
      />
      <Tabs
        items={[
          { key: "info", label: "基本信息", children: <Info app={app} editable={editable} /> },
          { key: "ops", label: "操作码", children: <Operations appId={id} editable={editable} /> },
          { key: "menus", label: "菜单", children: <Menus appId={id} editable={editable} /> },
          { key: "data", label: "数据编码", children: <DataTypes appId={id} editable={editable} /> },
          { key: "roles", label: "内置角色", children: <Roles appId={id} editable={me?.role === "ADMIN"} /> },
        ]}
      />
    </>
  );
}

// ───────────────────────────── 基本信息

function Info({ app, editable }: { app: AppInfo; editable: boolean }) {
  const act = useAct();
  const [edit, setEdit] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const keys = [["app", app.id], ["apps"]];
  return (
    <Card>
      {secret && (
        <div style={{ marginBottom: 16 }}>
          <OneTimeSecret label="新的 App Secret（旧的已失效）" value={secret} />
        </div>
      )}
      <Descriptions column={2} size="small">
        <Descriptions.Item label="名称">{app.name}</Descriptions.Item>
        <Descriptions.Item label="应用编码">{app.code ?? "—"}</Descriptions.Item>
        <Descriptions.Item label="状态">
          <Tag color={app.status === "active" ? "green" : "default"}>{app.status === "active" ? "启用" : "停用"}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="入口地址">{app.home_url ?? "—"}</Descriptions.Item>
        <Descriptions.Item label="App Key">
          {app.app_key ? <Typography.Text copyable>{app.app_key}</Typography.Text> : "—"}
        </Descriptions.Item>
        <Descriptions.Item label="App Secret">{app.secret_tail ? `…${app.secret_tail}` : "—"}</Descriptions.Item>
        <Descriptions.Item label="说明" span={2}>
          {app.description ?? "—"}
        </Descriptions.Item>
      </Descriptions>
      {editable && (
        <Space style={{ marginTop: 16 }}>
          <Button onClick={() => setEdit(true)}>编辑</Button>
          {app.status === "active" ? (
            <Popconfirm
              title="停用后所有租户都用不了这个应用，它调用户中心也会被拒"
              onConfirm={() => act(() => uc.post(`/apps/${app.id}/disable`), "已停用", keys)}
            >
              <Button danger>停用</Button>
            </Popconfirm>
          ) : (
            <Button onClick={() => act(() => uc.post(`/apps/${app.id}/enable`), "已启用", keys)}>启用</Button>
          )}
          <Popconfirm
            title="轮换后旧的 App Secret 立即失效"
            onConfirm={async () => {
              try {
                const out = await uc.post<{ app_secret: string }>(`/apps/${app.id}/rotate-secret`);
                setSecret(out.app_secret);
              } catch (e) {
                await act(() => Promise.reject(e), "", []);
              }
            }}
          >
            <Button>轮换 App Secret</Button>
          </Popconfirm>
        </Space>
      )}
      {edit && (
        <FormModal
          title="编辑应用"
          initial={app}
          fields={[
            { name: "name", label: "名称", required: true },
            { name: "code", label: "应用编码" },
            { name: "icon", label: "图标" },
            { name: "home_url", label: "入口地址" },
            { name: "description", label: "说明", textarea: true },
          ]}
          onClose={() => setEdit(false)}
          onSubmit={(v) => act(() => uc.patch(`/apps/${app.id}`, { ...v, version: app.version }), "已保存", keys)}
        />
      )}
    </Card>
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
    <Modal
      open
      title={title}
      onCancel={onClose}
      onOk={() => form.submit()}
      confirmLoading={busy}
    >
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
          <Form.Item key={f.name} name={f.name} label={f.label} extra={f.extra} rules={f.required ? [{ required: true }] : []}>
            {f.render ? f.render() : f.textarea ? <Input.TextArea rows={2} /> : <Input disabled={f.disabled} />}
          </Form.Item>
        ))}
      </Form>
    </Modal>
  );
}

// ───────────────────────────── 操作码

function Operations({ appId, editable }: { appId: string; editable: boolean }) {
  const act = useAct();
  const { modal } = App.useApp();
  const key = ["app-ops", appId];
  const q = useQuery({ queryKey: key, queryFn: () => uc.get<OpInfo[]>(`/apps/${appId}/operations`) });
  const [edit, setEdit] = useState<OpInfo | "new" | null>(null);

  async function remove(op: OpInfo) {
    try {
      const impact = await uc.del<{ roles: string[] }>(`/operations/${op.id}`, { dry_run: true });
      modal.confirm({
        title: `删除操作码 ${op.code}`,
        content: impact.roles.length
          ? `应用内置角色「${impact.roles.join("、")}」里的这个操作码会一起去掉；各租户自定义角色里的也会去掉。`
          : "各租户自定义角色里的这个操作码会一起去掉。",
        okButtonProps: { danger: true },
        onOk: () => act(() => uc.del(`/operations/${op.id}`), "已删除", [key, ["app", appId]]),
      });
    } catch (e) {
      await act(() => Promise.reject(e), "", []);
    }
  }

  return (
    <>
      {editable && (
        <Button type="primary" style={{ marginBottom: 12 }} onClick={() => setEdit("new")}>
          新增操作码
        </Button>
      )}
      <ErrorBox error={q.error} />
      <Table<OpInfo>
        rowKey="id"
        size="small"
        loading={q.isLoading}
        dataSource={q.data ?? []}
        pagination={false}
        columns={[
          { title: "模块", dataIndex: "module" },
          { title: "编码", dataIndex: "code", render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
          { title: "名称", dataIndex: "name" },
          { title: "所在的内置角色", dataIndex: "roles", render: (v: string[] = []) => v.join("、") || "—" },
          {
            title: "",
            width: 140,
            render: (_, op) =>
              editable && (
                <Space size={0}>
                  <Button type="link" size="small" onClick={() => setEdit(op)}>
                    编辑
                  </Button>
                  <Button type="link" size="small" danger onClick={() => remove(op)}>
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

function Menus({ appId, editable }: { appId: string; editable: boolean }) {
  const act = useAct();
  const { modal } = App.useApp();
  const key = ["app-menus", appId];
  const q = useQuery({ queryKey: key, queryFn: () => uc.get<MenuInfo[]>(`/apps/${appId}/menus`) });
  const [edit, setEdit] = useState<MenuInfo | "new" | null>(null);
  const menus = q.data ?? [];
  const dirs = menus.filter((m) => m.type === "dir");
  const nameOf = (id: string | null) => menus.find((m) => m.id === id)?.name ?? "—";

  async function remove(m: MenuInfo) {
    try {
      const impact = await uc.del<{ roles: string[] }>(`/menus/${m.id}`, { dry_run: true });
      modal.confirm({
        title: `删除菜单 ${m.name}`,
        content: impact.roles.length ? `内置角色「${impact.roles.join("、")}」上的这个菜单会一起去掉。` : "确定删除？",
        okButtonProps: { danger: true },
        onOk: () => act(() => uc.del(`/menus/${m.id}`), "已删除", [key, ["app", appId]]),
      });
    } catch (e) {
      await act(() => Promise.reject(e), "", []);
    }
  }

  return (
    <>
      {editable && (
        <Button type="primary" style={{ marginBottom: 12 }} onClick={() => setEdit("new")}>
          新增菜单
        </Button>
      )}
      <ErrorBox error={q.error} />
      <Table<MenuInfo>
        rowKey="id"
        size="small"
        loading={q.isLoading}
        dataSource={menus}
        pagination={false}
        columns={[
          { title: "名称", render: (_, m) => `${m.icon ?? ""} ${m.name}` },
          { title: "类型", dataIndex: "type", render: (v: string) => (v === "dir" ? "目录" : "菜单") },
          { title: "编码", dataIndex: "code", render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
          { title: "路径", dataIndex: "path", render: (v: string | null) => v ?? "—" },
          { title: "上级", dataIndex: "parent_id", render: nameOf },
          { title: "公共", dataIndex: "is_public", render: (v: boolean) => (v ? "是" : "") },
          { title: "绑定的角色数", dataIndex: "role_count" },
          {
            title: "",
            width: 200,
            render: (_, m) =>
              editable && (
                <Space size={0}>
                  <Button type="link" size="small" onClick={() => act(() => uc.post(`/menus/${m.id}/move-up`), "已上移", [key])}>
                    上移
                  </Button>
                  <Button type="link" size="small" onClick={() => setEdit(m)}>
                    编辑
                  </Button>
                  <Button type="link" size="small" danger onClick={() => remove(m)}>
                    删除
                  </Button>
                </Space>
              ),
          },
        ]}
      />
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
            {
              name: "parent_id",
              label: "上级目录",
              render: () => <Select allowClear options={dirs.map((d) => ({ value: d.id, label: d.name }))} />,
            },
            {
              name: "is_public",
              label: "公共菜单（能访问这个应用的人都看得到）",
              render: () => <Switch />,
            },
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

function DataTypes({ appId, editable }: { appId: string; editable: boolean }) {
  const act = useAct();
  const key = ["app-data-types", appId];
  const q = useQuery({ queryKey: key, queryFn: () => uc.get<DataTypeInfo[]>(`/apps/${appId}/data-types`) });
  const [edit, setEdit] = useState<DataTypeInfo | "new" | null>(null);
  return (
    <>
      {editable && (
        <Button type="primary" style={{ marginBottom: 12 }} onClick={() => setEdit("new")}>
          新增数据编码
        </Button>
      )}
      <ErrorBox error={q.error} />
      <Table<DataTypeInfo>
        rowKey="id"
        size="small"
        loading={q.isLoading}
        dataSource={q.data ?? []}
        pagination={false}
        columns={[
          { title: "编码", dataIndex: "code", render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
          { title: "名称", dataIndex: "name" },
          { title: "说明", dataIndex: "description" },
          { title: "各租户登记的数据条数", dataIndex: "data_count" },
          {
            title: "",
            width: 140,
            render: (_, d) =>
              editable && (
                <Space size={0}>
                  <Button type="link" size="small" onClick={() => setEdit(d)}>
                    编辑
                  </Button>
                  <Popconfirm
                    title="还有任何租户的数据在用这个编码就删不掉"
                    onConfirm={() => act(() => uc.del(`/data-types/${d.id}`), "已删除", [key, ["app", appId]])}
                  >
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
            {
              name: "code",
              label: "编码",
              required: true,
              disabled: edit !== "new",
              extra: "大写字母开头，字母和数字（如 AtlasAgent），要和应用代码一致",
            },
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

// ───────────────────────────── 内置角色

function Roles({ appId, editable }: { appId: string; editable: boolean }) {
  const act = useAct();
  const { modal } = App.useApp();
  const key = ["app-roles", appId];
  const q = useQuery({ queryKey: key, queryFn: () => uc.get<RoleInfo[]>(`/apps/${appId}/roles`) });
  const [edit, setEdit] = useState<RoleInfo | "new" | null>(null);
  const [grants, setGrants] = useState<RoleInfo | null>(null);

  async function remove(r: RoleInfo) {
    try {
      const impact = await uc.del<{ grants: number; positions: number }>(`/roles/${r.id}`, { dry_run: true });
      modal.confirm({
        title: `删除内置角色 ${r.name}`,
        content: `各租户里授了这个角色的 ${impact.grants} 条授权、${impact.positions} 个岗位上的默认角色会一起删掉。`,
        okButtonProps: { danger: true },
        onOk: () => act(() => uc.del(`/roles/${r.id}`), "已删除", [key, ["app", appId]]),
      });
    } catch (e) {
      await act(() => Promise.reject(e), "", []);
    }
  }

  return (
    <>
      <Typography.Paragraph type="secondary">
        内置角色所有租户共用，租户里改不了（可以复制成自己的角色再改）。租户自定义的角色不在这里。
      </Typography.Paragraph>
      {editable && (
        <Button type="primary" style={{ marginBottom: 12 }} onClick={() => setEdit("new")}>
          新增内置角色
        </Button>
      )}
      <ErrorBox error={q.error} />
      <Table<RoleInfo>
        rowKey="id"
        size="small"
        loading={q.isLoading}
        dataSource={q.data ?? []}
        pagination={false}
        columns={[
          { title: "编码", dataIndex: "code", render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
          { title: "名称", dataIndex: "name" },
          { title: "说明", dataIndex: "description" },
          { title: "操作码数", dataIndex: "operation_count" },
          {
            title: "",
            width: 240,
            render: (_, r) =>
              editable &&
              !r.is_builtin && (
                <Space size={0}>
                  <Button type="link" size="small" onClick={() => setGrants(r)}>
                    操作码与菜单
                  </Button>
                  <Button type="link" size="small" onClick={() => setEdit(r)}>
                    编辑
                  </Button>
                  <Button type="link" size="small" danger onClick={() => remove(r)}>
                    删除
                  </Button>
                </Space>
              ),
          },
        ]}
      />
      {edit && (
        <FormModal
          title={edit === "new" ? "新增内置角色" : `编辑 ${edit.code}`}
          initial={edit === "new" ? {} : edit}
          fields={[
            {
              name: "code",
              label: "编码",
              required: true,
              disabled: edit !== "new",
              extra: "大写字母开头，大写字母、数字、下划线（如 ATLAS_MEMBER）",
            },
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
      {grants && <RoleGrants appId={appId} role={grants} onClose={() => setGrants(null)} />}
    </>
  );
}

function RoleGrants({ appId, role, onClose }: { appId: string; role: RoleInfo; onClose: () => void }) {
  const act = useAct();
  const detail = useQuery({ queryKey: ["role", role.id], queryFn: () => uc.get<RoleInfo>(`/roles/${role.id}`) });
  const ops = useQuery({ queryKey: ["app-ops", appId], queryFn: () => uc.get<OpInfo[]>(`/apps/${appId}/operations`) });
  const menus = useQuery({ queryKey: ["app-menus", appId], queryFn: () => uc.get<MenuInfo[]>(`/apps/${appId}/menus`) });
  const [opIds, setOpIds] = useState<string[] | null>(null);
  const [menuIds, setMenuIds] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const curOps = opIds ?? detail.data?.operation_ids ?? [];
  const curMenus = menuIds ?? detail.data?.menu_ids ?? [];

  async function save() {
    setBusy(true);
    const keys = [["app-roles", appId], ["role", role.id], ["app-ops", appId], ["app-menus", appId]];
    const ok =
      (await act(() => uc.put(`/roles/${role.id}/operations`, { ids: curOps }), "操作码已保存", keys)) &&
      (await act(() => uc.put(`/roles/${role.id}/menus`, { ids: curMenus }), "菜单已保存", keys));
    setBusy(false);
    if (ok) onClose();
  }

  return (
    <Modal open width={640} title={`${role.name} · 操作码与菜单`} onCancel={onClose} onOk={save} confirmLoading={busy}>
      <ErrorBox error={detail.error ?? ops.error ?? menus.error} />
      <Typography.Text strong>操作码</Typography.Text>
      <Checkbox.Group
        style={{ display: "flex", flexDirection: "column", gap: 4, margin: "8px 0 16px" }}
        value={curOps}
        onChange={(v) => setOpIds(v as string[])}
        options={(ops.data ?? []).map((o) => ({ value: o.id, label: `${o.module} · ${o.name}（${o.code}）` }))}
      />
      <Typography.Text strong>菜单</Typography.Text>
      <Checkbox.Group
        style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 8 }}
        value={curMenus}
        onChange={(v) => setMenuIds(v as string[])}
        options={(menus.data ?? [])
          .filter((m) => m.type === "menu" && !m.is_public)
          .map((m) => ({ value: m.id, label: `${m.icon ?? ""} ${m.name}（${m.path}）` }))}
      />
    </Modal>
  );
}
