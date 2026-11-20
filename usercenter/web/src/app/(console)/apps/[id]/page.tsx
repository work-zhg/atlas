"use client";

/**
 * 应用详情（租户视角）：可访问范围 / 操作 / 菜单 / 数据编码。
 * ★ 应用目录（接入、凭据、操作码、菜单、数据编码、应用内置角色）由运营中心维护，这里只读。
 *   租户能改的：本租户的可访问范围、「全部数据」授权（数据权限）、本租户自定义的角色。
 * ★ 角色在「权限 › 角色」集中管理（「操作」页签有入口，筛好本应用）。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Descriptions, Dropdown, Modal, Radio, Select, Space, Table, Tabs, Tag } from "antd";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { AppIcon, DeptSelect, DetailHead, LevelTag, SubjectPicker, toSubjects, UserSelect, type SubjectValue } from "@/components/common";
import { Icon } from "@/components/Icon";
import { menuRows } from "@/components/roles";
import { api, errorText } from "@/lib/api";
import { fmtTime, LEVEL } from "@/lib/format";
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
    // 可访问范围属于本租户（tenant_app:manage）
    !a.is_builtin && can("tenant_app:manage") && { key: "conf", label: "可访问范围", children: <ConfTab app={a} /> },
    { key: "ops", label: `操作 ${a.operations}`, children: <OpsTab app={a} /> },
    { key: "menus", label: `菜单 ${a.menus}`, children: <MenusTab app={a} /> },
    // 数据编码只读；「全部数据」授权是本租户的数据权限
    !a.is_builtin && (can("data:view") || can("data:type_grant")) && { key: "dtypes", label: `数据编码 ${a.data_types}`, children: <DataTypesTab app={a} /> },
  ].filter(Boolean) as { key: string; label: string; children: React.ReactNode }[];

  return (
    <>
      <DetailHead
        back="/apps"
        icon={<AppIcon app={a} size={44} />}
        title={a.name}
        tags={a.is_builtin ? <Tag>内置</Tag> : a.status === "active" ? <Tag color="success">已启用</Tag> : <Tag>已停用</Tag>}
        sub={a.is_builtin ? "本系统 · 操作与菜单随版本发布（只读）" : `${a.description ?? ""}${a.description ? " · " : ""}应用目录由运营中心维护`}
      />
      {a.status === "disabled" && <Alert type="warning" showIcon style={{ margin: "8px 0 4px" }} message="应用已被运营中心停用：开放接口拒绝该应用的调用。" />}
      <Tabs items={tabs} destroyOnHidden />
    </>
  );
}

// ───────────────────────────────────────────── 接入配置

function ConfTab({ app }: { app: AppInfo }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [all, setAll] = useState(app.scope_all);
  const [depts, setDepts] = useState<string[]>(app.scope_dept_ids ?? []);
  const refresh = () => (qc.invalidateQueries({ queryKey: ["app", app.id] }), qc.invalidateQueries({ queryKey: ["apps"] }));
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
  const ops = useQuery({ queryKey: ["app", app.id, "ops"], queryFn: () => api.get<Operation[]>(`/apps/${app.id}/operations`) });
  const first = ops.data?.[0]?.code ?? "xxx:view";
  return (
    <Card
      size="small"
      title="操作目录"
      extra={
        <Space>
          <span className="uc-sub">该应用可被授权的操作{app.is_builtin ? " · 内置应用只读" : " · 由运营中心维护"}</span>
          {can("role:view") && <Link href={`/perm/roles?app=${app.id}`}>用这些操作配角色：权限 › 角色 →</Link>}
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
        ]}
      />
      <div className="uc-muted" style={{ margin: "12px 0 6px" }}>
        前端按操作码控制权限（示例）：
      </div>
      <pre className="uc-code">{`// 用户登录应用后，应用后端调用 GET /open/v1/users/{id}/authz 取得本应用的操作码
const can = (op) => permissions.includes(op);
{can('${first}') && <Button>…</Button>}   // 没有权限：按钮不显示
// 后端接口同样按操作码校验，前端控制只负责「看不到」`}</pre>
    </Card>
  );
}

// ───────────────────────────────────────────── 菜单

function MenusTab({ app }: { app: AppInfo }) {
  const menus = useQuery({ queryKey: ["app", app.id, "menus"], queryFn: () => api.get<MenuItem[]>(`/apps/${app.id}/menus`) });
  const [pvUser, setPvUser] = useState<string>();
  const preview = useQuery({
    queryKey: ["app", app.id, "preview", pvUser],
    queryFn: () => api.get<{ can_access: boolean; roles: string[]; menus: MenuNode[] }>(`/apps/${app.id}/menus/preview`, { user_id: pvUser }),
    enabled: !!pvUser,
  });
  const rows = menuRows(menus.data ?? []);

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
            <span className="uc-muted">{app.is_builtin ? "内置菜单随版本发布" : "由运营中心维护；在「权限 › 角色 › 菜单权限」里绑定给本租户的角色"}</span>
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
    </div>
  );
}

// ───────────────────────────────────────────── 数据编码

function DataTypesTab({ app }: { app: AppInfo }) {
  const list = useQuery({ queryKey: ["app", app.id, "dtypes"], queryFn: () => api.get<DataTypeInfo[]>(`/apps/${app.id}/data-types`) });
  const [typeAcl, setTypeAcl] = useState<DataTypeInfo | null>(null);
  const code = list.data?.[0]?.code ?? "Agent";
  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card size="small" title="数据编码" extra={<span className="uc-sub">由运营中心维护</span>}>
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
