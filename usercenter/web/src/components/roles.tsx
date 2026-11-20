"use client";

/**
 * 角色（「权限 › 角色」）：角色属于应用，只能包含该应用的操作与菜单。
 * 原来在「应用 › 应用详情 › 角色」页签里，挪到权限下集中管理（应用详情的「操作」页签有入口跳过来）。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Checkbox, Dropdown, Empty, Form, Input, Modal, Select, Space, Table, Tabs, Tag } from "antd";
import { useEffect, useMemo, useState } from "react";

import { AppIcon, PersonCell, StatusTag } from "@/components/common";
import { GrantToModal, useGrantMutations } from "@/components/grantActions";
import { Icon } from "@/components/Icon";
import { api, errorText } from "@/lib/api";
import { useCan } from "@/lib/session";
import type { AppInfo, GrantRow, MenuItem, Operation, Role } from "@/lib/types";

export function menuRows(items: MenuItem[], parent: string | null = null, depth = 0): (MenuItem & { depth: number })[] {
  return items
    .filter((m) => m.parent_id === parent)
    .sort((a, b) => a.sort - b.sort)
    .flatMap((m) => [{ ...m, depth }, ...menuRows(items, m.id, depth + 1)]);
}

/** 新建 / 编辑 / 复制角色。新建时没指定应用就让人选（角色属于应用）。 */
export function RoleForm({
  app,
  apps,
  mode,
  role,
  onClose,
  onDone,
}: {
  app?: AppInfo;
  apps?: AppInfo[];
  mode: "new" | "edit" | "copy";
  role: Role | null;
  onClose: () => void;
  onDone: (id: string, appId: string) => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  async function submit(v: { name: string; code: string; description?: string; app_id?: string }) {
    const appId = app?.id ?? v.app_id!;
    try {
      if (mode === "edit" && role) {
        await api.patch(`/roles/${role.id}`, { name: v.name, description: v.description });
        onDone(role.id, appId);
      } else {
        const r = await api.post<Role>(`/apps/${appId}/roles`, { name: v.name, code: v.code, description: v.description, copy_from: mode === "copy" ? role?.id : undefined });
        onDone(r.id, appId);
      }
      onClose();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open title={mode === "edit" ? `编辑角色 · ${role?.name}` : mode === "copy" ? `复制角色 · ${role?.name}` : "新建角色"} onCancel={onClose} onOk={() => form.submit()} okText="保存">
      <Form form={form} layout="vertical" onFinish={submit} initialValues={mode === "edit" ? role ?? {} : mode === "copy" ? { name: `${role?.name}（副本）`, description: role?.description } : {}}>
        {app ? (
          <div className="uc-muted" style={{ marginBottom: 12 }}>
            所属应用：{app.name}（角色只能包含该应用的操作与菜单）
          </div>
        ) : (
          <Form.Item name="app_id" label="所属应用" extra="角色只能包含该应用的操作与菜单，创建后不能换应用" rules={[{ required: true, message: "请选择应用" }]}>
            <Select options={(apps ?? []).map((a) => ({ value: a.id, label: a.name }))} placeholder="选择应用" />
          </Form.Item>
        )}
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

/** 一个角色的详情：操作权限 / 菜单权限 / 授权 / 拥有者 */
export function RoleDetail({ app, roleId, onEdit, onDeleted }: { app: AppInfo; roleId: string; onEdit: (mode: "edit" | "copy") => void; onDeleted: () => void }) {
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
  // 应用内置角色（所有租户共用）由运营中心维护：租户里只能看、复制
  const editable = !!r && can("role:manage") && !r.is_builtin && !r.shared;
  const modules = useMemo(() => {
    const m = new Map<string, Operation[]>();
    for (const o of ops.data ?? []) m.set(o.module, [...(m.get(o.module) ?? []), o]);
    return [...m.entries()];
  }, [ops.data]);
  const refreshLists = () => (qc.invalidateQueries({ queryKey: ["app", app.id] }), qc.invalidateQueries({ queryKey: ["roles"] }));
  const save = useMutation({
    mutationFn: (kind: "operations" | "menus") => api.put(`/roles/${roleId}/${kind}`, { ids: kind === "operations" ? opIds : menuIds }),
    onSuccess: () => (qc.invalidateQueries({ queryKey: ["role", roleId] }), refreshLists(), message.success("已保存；拥有者的权限随之更新")),
    onError: (e) => message.error(errorText(e)),
  });
  if (!r) return <Card loading />;

  async function remove() {
    const pre = await api.del<{ grants: number; positions: string[]; holders: number }>(`/roles/${roleId}`, { dry_run: true });
    modal.confirm({
      title: `删除角色 · ${r!.name}`,
      content: `将撤销 ${pre.grants} 条授权${pre.positions.length ? `，并从岗位 ${pre.positions.join("、")} 的默认角色中移除` : ""}；目前 ${pre.holders} 人拥有该角色。`,
      okText: "删除",
      okButtonProps: { danger: true },
      onOk: async () => (await api.del(`/roles/${roleId}`), refreshLists(), onDeleted(), message.success("已删除")),
    });
  }

  const menuList = menuRows(menus.data ?? []);
  const descendants = (id: string): string[] => (menus.data ?? []).filter((m) => m.parent_id === id).flatMap((m) => [m.id, ...descendants(m.id)]);
  const saveBar = (kind: "operations" | "menus") =>
    editable ? (
      <div className="uc-row" style={{ marginTop: 12 }}>
        <span className="uc-sub">保存后，拥有该角色的 {holders.data?.length ?? 0} 人在应用下次刷新授权时生效</span>
        <div className="uc-grow" />
        <Button type="primary" onClick={() => save.mutate(kind)} loading={save.isPending}>
          保存
        </Button>
      </div>
    ) : (
      <div className="uc-sub" style={{ marginTop: 12 }}>
        {r.is_builtin
          ? "内置角色随版本发布，不能修改"
          : r.shared
            ? "应用内置角色由运营中心维护，所有租户共用；可以复制成本租户的角色再改"
            : "你只能查看角色；修改需要「管理角色」权限"}
      </div>
    );

  return (
    <Card>
      <div className="uc-row" style={{ alignItems: "flex-start", gap: 10 }}>
        <div className="uc-grow">
          <div className="uc-row" style={{ flexWrap: "wrap" }}>
            <span style={{ fontSize: 18, fontWeight: 650 }}>{r.name}</span>
            <span className="uc-mono uc-muted">{r.code}</span>
            {(r.is_builtin || r.shared) && <Tag>内置</Tag>}
          </div>
          <div className="uc-row uc-sub" style={{ marginTop: 4 }}>
            <AppIcon app={app} size={18} />
            {app.name}
            {r.description && <span>· {r.description}</span>}
          </div>
        </div>
        {can("role:manage") && (
          <Button size="small" icon={<Icon name="copy" size={14} />} onClick={() => onEdit("copy")}>
            复制
          </Button>
        )}
        {editable && (
          <Button size="small" icon={<Icon name="pencil" size={14} />} onClick={() => onEdit("edit")}>
            编辑
          </Button>
        )}
        {editable && (
          <Dropdown trigger={["click"]} menu={{ items: [{ key: "del", danger: true, icon: <Icon name="trash" size={14} />, label: "删除角色", onClick: () => void remove() }] }}>
            <Button type="text" size="small" aria-label="更多" icon={<Icon name="more" />} />
          </Dropdown>
        )}
      </div>
      <Tabs
        style={{ marginTop: 8 }}
        items={[
          {
            key: "ops",
            label: `操作权限 ${opIds.length}`,
            children: (
              <>
                {modules.map(([mod, list]) => (
                  <div key={mod} style={{ border: "1px solid var(--line)", borderRadius: 10, marginBottom: 10, overflow: "hidden" }}>
                    <div style={{ padding: "8px 12px", background: "var(--bg-side)", borderBottom: "1px solid var(--line)" }}>
                      <Checkbox
                        disabled={!editable}
                        checked={list.every((o) => opIds.includes(o.id))}
                        indeterminate={list.some((o) => opIds.includes(o.id)) && !list.every((o) => opIds.includes(o.id))}
                        onChange={(e) => setOpIds(e.target.checked ? [...new Set([...opIds, ...list.map((o) => o.id)])] : opIds.filter((x) => !list.some((o) => o.id === x)))}
                      >
                        <b>{mod}</b> <span className="uc-sub">{list.filter((o) => opIds.includes(o.id)).length}/{list.length}</span>
                      </Checkbox>
                    </div>
                    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px 12px", padding: "10px 12px" }}>
                      {list.map((o) => (
                        <Checkbox key={o.id} disabled={!editable} checked={opIds.includes(o.id)} onChange={(e) => setOpIds(e.target.checked ? [...opIds, o.id] : opIds.filter((x) => x !== o.id))}>
                          {o.name} <span className="uc-mono uc-muted">{o.code}</span>
                        </Checkbox>
                      ))}
                    </div>
                  </div>
                ))}
                {!modules.length && <Empty description="该应用还没有登记操作" />}
                {saveBar("operations")}
              </>
            ),
          },
          {
            key: "menus",
            label: `菜单权限 ${menuIds.length}`,
            children: (
              <>
                {menuList.map((m) => {
                  const kids = descendants(m.id).filter((id) => (menus.data ?? []).find((x) => x.id === id)?.type === "menu");
                  const checked = m.is_public || menuIds.includes(m.id) || (m.type === "dir" && kids.some((k) => menuIds.includes(k) || (menus.data ?? []).find((x) => x.id === k)?.is_public));
                  return (
                    <div key={m.id} style={{ paddingLeft: m.depth * 22, padding: "3px 0 3px " + m.depth * 22 + "px" }}>
                      <Checkbox
                        disabled={!editable || m.is_public}
                        checked={checked}
                        onChange={(e) => {
                          const ids = m.type === "dir" ? kids : [m.id];
                          setMenuIds(e.target.checked ? [...new Set([...menuIds, ...ids])] : menuIds.filter((x) => !ids.includes(x)));
                        }}
                      >
                        {m.type === "dir" ? <b>{m.name}</b> : m.name} {m.path && <span className="uc-mono uc-muted">{m.path}</span>} {m.type === "dir" && <Tag>目录</Tag>}
                        {m.is_public && <Tag color="green">公共</Tag>}
                      </Checkbox>
                    </div>
                  );
                })}
                {!menuList.length && <Empty description="该应用还没有菜单" />}
                <div className="uc-sub" style={{ marginTop: 8 }}>
                  公共菜单所有人可见，无需绑定；目录只在至少有一个可见子菜单时显示。
                </div>
                {saveBar("menus")}
              </>
            ),
          },
          {
            key: "grants",
            label: `授权 ${grants.data?.grants.length ?? 0}`,
            children: (
              <Space direction="vertical" size={14} style={{ width: "100%" }}>
                <Card
                  size="small"
                  title="直接授予的人和组织"
                  styles={{ body: { padding: 0 } }}
                  extra={
                    can("grant:manage") && (
                      <Button size="small" type="primary" icon={<Icon name="plus" size={14} />} onClick={() => setGrantTo(true)}>
                        授权
                      </Button>
                    )
                  }
                >
                  <Table<GrantRow>
                    size="small"
                    rowKey="id"
                    pagination={false}
                    dataSource={grants.data?.grants ?? []}
                    locale={{ emptyText: "还没有直接授予" }}
                    columns={[
                      { title: "对象", render: (_, g) => <span className="uc-row"><Icon name={g.subject.type === "user" ? "user" : "building"} size={14} />{g.subject.name}</span> },
                      { title: "范围", render: (_, g) => (g.subject.type === "user" ? "本人" : g.include_sub ? "本部门及下级部门" : "仅本部门") },
                      { title: "覆盖人数", dataIndex: "covered" },
                      { title: "", align: "right", render: (_, g) => can("grant:manage") && <Button type="text" size="small" danger onClick={() => grantActs.revoke(g)}>撤销</Button> },
                    ]}
                  />
                </Card>
                <Card size="small" title="通过岗位授予" extra={<span className="uc-sub">以下岗位的默认角色包含「{r.name}」</span>}>
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
            label: `拥有者 ${holders.data?.length ?? 0}`,
            children: (
              <Table
                size="small"
                rowKey={(h) => h.user.id}
                pagination={false}
                dataSource={holders.data ?? []}
                columns={[
                  { title: "拥有者", render: (_, h) => <PersonCell name={h.user.name} account={h.user.account} extra={h.user.status === "disabled" && <StatusTag status="disabled" />} /> },
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
