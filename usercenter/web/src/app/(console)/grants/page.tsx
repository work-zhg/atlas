"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Avatar, Button, Card, Checkbox, Empty, Form, Input, Modal, Segmented, Select, Space, Table, Tabs, Tag, Tree } from "antd";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";

import { buildDeptTree, LevelTag, PageHead, StatusTag, useDirectoryDepts, UserSelect } from "@/components/common";
import { GrantToModal, useGrantMutations } from "@/components/grantActions";
import { RoleChecks, usePositions } from "@/components/grantPickers";
import { api, errorText } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useCan } from "@/lib/session";
import type { DataRow, DataTypeInfo, GrantRow, Page, PositionRow, User } from "@/lib/types";

export default function GrantsPage() {
  return (
    <Suspense>
      <GrantsInner />
    </Suspense>
  );
}

function GrantsInner() {
  const can = useCan();
  const params = useSearchParams();
  const subjectParam = params.get("subject");
  const tabs = [
    can("grant:view") && { key: "role", label: "角色授权" },
    { key: "data", label: "数据授权" },
    can("position:view") && { key: "pos", label: "岗位管理" },
  ].filter(Boolean) as { key: string; label: string }[];
  const [tab, setTab] = useState(params.get("tab") ?? (subjectParam && can("grant:view") ? "role" : tabs[0]!.key));
  const current = tabs.some((t) => t.key === tab) ? tab : tabs[0]!.key;
  const sub = {
    role: "把岗位和角色授予用户或组织，决定能用应用的哪些功能。授予部门时可包含下级部门；用户的有效授权 = 本人 + 所在部门 + 上级部门（含下级）。",
    data: "按「数据编码 + 数据 Id」把只读 / 读写 / Owner 授给用户或组织。应用创建数据时写入创建人的 Owner，之后只有 Owner 能修改这条数据的授权。",
    pos: "岗位统一维护，可关联默认角色；在「角色授权」中把岗位授给用户或组织，获得岗位的人自动拥有其默认角色。",
  }[current];
  return (
    <>
      <PageHead title="权限管理" sub={sub} />
      <Tabs
        activeKey={current}
        onChange={setTab}
        items={tabs.map((t) => ({
          key: t.key,
          label: t.label,
          children: t.key === "role" ? <RoleGrants initialSubject={subjectParam} /> : t.key === "data" ? <DataTab /> : <PositionsTab />,
        }))}
      />
    </>
  );
}

// ───────────────────────────────────────────── 角色授权

type Subject = { type: "user" | "dept"; id: string };

function parseSubject(raw: string | null): Subject | null {
  if (!raw) return null;
  const [type, id] = raw.split(":");
  return (type === "user" || type === "dept") && id ? { type, id } : null;
}

function RoleGrants({ initialSubject }: { initialSubject: string | null }) {
  const [mode, setMode] = useState<"subject" | "list">("subject");
  return (
    <>
      <Segmented
        style={{ marginBottom: 12 }}
        value={mode}
        onChange={(v) => setMode(v as "subject" | "list")}
        options={[
          { value: "subject", label: "按对象授权" },
          { value: "list", label: "全部授权" },
        ]}
      />
      {mode === "subject" ? <BySubject initial={parseSubject(initialSubject)} /> : <GrantList />}
    </>
  );
}

function BySubject({ initial }: { initial: Subject | null }) {
  const [mode, setMode] = useState<"dept" | "user">(initial?.type ?? "dept");
  const [subject, setSubject] = useState<Subject | null>(initial);
  const depts = useDirectoryDepts();
  const rootId = depts.data?.find((d) => !d.parent_id)?.id;
  const current = subject ?? (mode === "dept" && rootId ? { type: "dept" as const, id: rootId } : null);
  const treeData = useMemo(() => buildDeptTree(depts.data ?? [], (d) => <span>{d.parent_id ? "📁" : "🏢"} {d.name}</span>), [depts.data]);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "300px minmax(0,1fr)", gap: 16, alignItems: "start" }}>
      <Card size="small">
        <Segmented
          block
          value={mode}
          onChange={(v) => (setMode(v as "dept" | "user"), setSubject(null))}
          options={[
            { value: "dept", label: "组织" },
            { value: "user", label: "用户" },
          ]}
          style={{ marginBottom: 12 }}
        />
        {mode === "dept" ? (
          treeData.length > 0 && (
            <Tree
              treeData={treeData}
              defaultExpandAll
              selectedKeys={current?.type === "dept" ? [current.id] : []}
              onSelect={(k) => k[0] && setSubject({ type: "dept", id: String(k[0]) })}
            />
          )
        ) : (
          <UserSelect value={current?.type === "user" ? current.id : undefined} onChange={(v) => v && setSubject({ type: "user", id: v as string })} />
        )}
      </Card>
      {current ? <SubjectPanel subject={current} onJump={(s) => (setMode(s.type), setSubject(s))} /> : <Card><Empty description="请选择用户" /></Card>}
    </div>
  );
}

function SubjectPanel({ subject, onJump }: { subject: Subject; onJump: (s: Subject) => void }) {
  const can = useCan();
  const actions = useGrantMutations();
  const [adding, setAdding] = useState<null | "role" | "position">(null);
  const data = useQuery({
    queryKey: ["subject-grants", subject.type, subject.id],
    queryFn: () => api.get<{ own: GrantRow[]; inherited: GrantRow[] }>(`/subjects/${subject.type}/${subject.id}/grants`),
  });
  const depts = useDirectoryDepts();
  const user = useQuery({
    queryKey: ["user", subject.id],
    queryFn: () => api.get<User>(`/users/${subject.id}`),
    enabled: subject.type === "user" && can("user:view"),
  });
  const deptName = depts.data?.find((d) => d.id === subject.id)?.name;
  const own = data.data?.own ?? [];
  const inherited = data.data?.inherited ?? [];

  const table = (kind: "role" | "position") => {
    const rows = [...own, ...inherited].filter((g) => g.kind === kind);
    return (
      <Table<GrantRow>
        size="small"
        rowKey="id"
        pagination={false}
        dataSource={rows}
        locale={{ emptyText: kind === "role" ? "没有角色" : "没有岗位" }}
        columns={[
          { title: kind === "role" ? "角色" : "岗位", render: (_, g) => <b>{g.target.name}</b> },
          ...(kind === "role" ? [{ title: "所属应用", render: (_: unknown, g: GrantRow) => g.target.app }] : []),
          {
            title: "来源",
            render: (_, g) =>
              own.includes(g) ? (
                subject.type === "dept" ? `本部门授权 · ${g.include_sub ? "含下级部门" : "仅本部门"}` : "直接授权"
              ) : (
                <span>
                  继承自{" "}
                  <a onClick={() => onJump({ type: "dept", id: g.subject.id })}>🏢 {g.subject.name}</a>
                  {g.include_sub ? "（含下级）" : ""}
                </span>
              ),
          },
          { title: "授权人 · 时间", render: (_, g) => <span className="uc-muted">{g.granted_by ?? "系统"} · {fmtTime(g.granted_at)}</span> },
          {
            title: "",
            align: "right",
            render: (_, g) =>
              own.includes(g) ? (
                can("grant:manage") && (
                  <Space size={0}>
                    {g.subject.type === "dept" && (
                      <Button type="link" size="small" onClick={() => actions.toggleSub(g)}>
                        {g.include_sub ? "改为仅本部门" : "改为含下级"}
                      </Button>
                    )}
                    <Button type="link" size="small" danger onClick={() => actions.revoke(g)}>
                      撤销
                    </Button>
                  </Space>
                )
              ) : (
                <span className="uc-muted">在来源处管理</span>
              ),
          },
        ]}
      />
    );
  };

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {subject.type === "dept" ? (
            <div>
              <h2 style={{ margin: 0, fontSize: 19 }}>🏢 {deptName}</h2>
              <div className="uc-muted">授予本部门的岗位、角色，部门成员自动获得；选择「含下级」时下级部门成员也获得。</div>
            </div>
          ) : (
            <Space>
              <Avatar style={{ background: "#6366f1" }}>{user.data?.name.slice(-1) ?? "?"}</Avatar>
              <div>
                <Space>
                  <b style={{ fontSize: 17 }}>{user.data?.name ?? "用户"}</b>
                  {user.data && <StatusTag status={user.data.status} />}
                </Space>
                <div className="uc-muted">
                  {user.data?.account} · {user.data?.dept_path?.join(" / ")}
                </div>
              </div>
            </Space>
          )}
          <div className="uc-grow" />
          {can("grant:manage") && (
            <Space>
              <Button onClick={() => setAdding("position")}>＋ 授予岗位</Button>
              <Button type="primary" onClick={() => setAdding("role")}>
                ＋ 授予角色
              </Button>
            </Space>
          )}
        </div>
      </Card>
      <Card size="small" title="岗位" extra={<span className="uc-muted">岗位的默认角色随岗位一起生效</span>} loading={data.isLoading}>
        {table("position")}
      </Card>
      <Card size="small" title="角色" loading={data.isLoading}>
        {table("role")}
      </Card>
      {subject.type === "user" && can("user:view") && (
        <div style={{ textAlign: "right" }}>
          <Link href={`/users/${subject.id}`}>查看有效角色与操作明细 →</Link>
        </div>
      )}
      {adding && (
        <GrantItemsModal
          kind={adding}
          subject={subject}
          granted={own.filter((g) => g.kind === adding).map((g) => g.target.id)}
          onClose={() => setAdding(null)}
          onDone={actions.refresh}
        />
      )}
    </Space>
  );
}

function GrantItemsModal({
  kind,
  subject,
  granted,
  onClose,
  onDone,
}: {
  kind: "role" | "position";
  subject: Subject;
  granted: string[];
  onClose: () => void;
  onDone: () => void;
}) {
  const { message } = App.useApp();
  const [ids, setIds] = useState<string[]>([]);
  const [includeSub, setIncludeSub] = useState(true);
  const positions = usePositions(kind === "position");
  async function ok() {
    if (!ids.length) return message.warning(`请选择要授予的${kind === "role" ? "角色" : "岗位"}`);
    try {
      const r = await api.post<{ added: number; updated: number }>("/grants", {
        kind,
        target_ids: ids,
        subjects: [subject],
        include_sub: includeSub,
      });
      onDone();
      message.success(`已授予 ${r.added} 项`);
      onClose();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open title={kind === "role" ? "授予角色" : "授予岗位"} onCancel={onClose} onOk={ok} okText="授权" width={560}>
      {kind === "role" ? (
        <RoleChecks value={ids} onChange={setIds} disabled={granted} />
      ) : (
        <div style={{ maxHeight: 360, overflow: "auto" }}>
          {(positions.data ?? []).map((p) => (
            <div key={p.id}>
              <Checkbox
                checked={ids.includes(p.id) || granted.includes(p.id)}
                disabled={granted.includes(p.id)}
                onChange={(e) => setIds(e.target.checked ? [...ids, p.id] : ids.filter((x) => x !== p.id))}
              >
                {p.name} <span className="uc-muted">{p.roles.map((r) => r.name).join("、") || "无默认角色"}</span>
              </Checkbox>
            </div>
          ))}
          {!positions.data?.length && <Empty description="还没有岗位" />}
        </div>
      )}
      {subject.type === "dept" && (
        <Alert
          style={{ marginTop: 12 }}
          type="info"
          message={
            <Checkbox checked={includeSub} onChange={(e) => setIncludeSub(e.target.checked)}>
              包含下级部门（下级部门的成员也获得）
            </Checkbox>
          }
        />
      )}
    </Modal>
  );
}

function GrantList() {
  const can = useCan();
  const actions = useGrantMutations();
  const [kind, setKind] = useState("all");
  const [st, setSt] = useState("all");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const list = useQuery({
    queryKey: ["grants", kind, st, q, page],
    queryFn: () =>
      api.get<Page<GrantRow>>("/grants", { kind: kind === "all" ? undefined : kind, subject_type: st === "all" ? undefined : st, q, page, size: 50 }),
  });
  return (
    <Card
      size="small"
      title={
        <Space wrap>
          <Segmented value={kind} onChange={(v) => (setKind(String(v)), setPage(1))} options={[{ value: "all", label: "全部" }, { value: "position", label: "岗位" }, { value: "role", label: "角色" }]} />
          <Segmented value={st} onChange={(v) => (setSt(String(v)), setPage(1))} options={[{ value: "all", label: "全部对象" }, { value: "user", label: "用户" }, { value: "dept", label: "组织" }]} />
        </Space>
      }
      extra={<Input.Search allowClear placeholder="搜索岗位、角色、用户、部门" onSearch={(v) => (setQ(v), setPage(1))} style={{ width: 240 }} />}
    >
      <Table<GrantRow>
        size="small"
        rowKey="id"
        loading={list.isLoading}
        dataSource={list.data?.items ?? []}
        pagination={{ current: page, pageSize: 50, total: list.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
        columns={[
          {
            title: "授权内容",
            render: (_, g) => (
              <Space>
                <Tag color={g.kind === "position" ? "blue" : "purple"}>{g.kind === "position" ? "岗位" : "角色"}</Tag>
                <b>{g.target.name}</b>
                {g.target.app && <span className="uc-muted">{g.target.app}</span>}
              </Space>
            ),
          },
          {
            title: "授权对象",
            render: (_, g) => (
              <Link href={`/grants?subject=${g.subject.type}:${g.subject.id}`}>
                {g.subject.type === "user" ? "👤" : "🏢"} {g.subject.name}
              </Link>
            ),
          },
          { title: "范围", render: (_, g) => (g.subject.type === "user" ? "本人" : g.include_sub ? "本部门及下级部门" : "仅本部门") },
          { title: "覆盖人数", dataIndex: "covered" },
          { title: "授权人 · 时间", render: (_, g) => <span className="uc-muted">{g.granted_by ?? "系统"} · {fmtTime(g.granted_at)}</span> },
          {
            title: "",
            align: "right",
            render: (_, g) =>
              can("grant:manage") && (
                <Space size={0}>
                  {g.subject.type === "dept" && (
                    <Button type="link" size="small" onClick={() => actions.toggleSub(g)}>
                      {g.include_sub ? "改为仅本部门" : "改为含下级"}
                    </Button>
                  )}
                  <Button type="link" size="small" danger onClick={() => actions.revoke(g)}>
                    撤销
                  </Button>
                </Space>
              ),
          },
        ]}
      />
    </Card>
  );
}

// ───────────────────────────────────────────── 数据授权

function DataTab() {
  const can = useCan();
  const router = useRouter();
  const [scope, setScope] = useState("mine");
  const [code, setCode] = useState<string>();
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const types = useQuery({ queryKey: ["data-types"], queryFn: () => api.get<DataTypeInfo[]>("/data-types") });
  const list = useQuery({
    queryKey: ["data", scope, code, q, page],
    queryFn: () => api.get<Page<DataRow> & { counts: Record<string, number> }>("/data", { scope, data_code: code, q, page, size: 50 }),
  });
  const counts = list.data?.counts ?? {};
  const opts = [
    { value: "mine", label: `我管理的 ${counts.mine ?? 0}` },
    { value: "access", label: `我可访问的 ${counts.access ?? 0}` },
    ...(can("data:view") ? [{ value: "all", label: `全部数据 ${counts.all ?? 0}` }] : []),
  ];
  const empty = {
    mine: "你还不是任何数据的 Owner。应用（如 Atlas）创建数据时会把创建人登记为 Owner。",
    access: "还没有人把数据授权给你（或你所在的部门）。",
    all: "暂无数据",
  }[scope];
  return (
    <>
      <Segmented style={{ marginBottom: 12 }} value={scope} onChange={(v) => (setScope(String(v)), setPage(1))} options={opts} />
      <Card
        size="small"
        title={
          <Select
            allowClear
            placeholder="全部数据编码"
            style={{ width: 240 }}
            value={code}
            onChange={(v) => (setCode(v), setPage(1))}
            options={(types.data ?? []).map((t) => ({ value: t.code, label: `${t.code} · ${t.name}` }))}
          />
        }
        extra={<Input.Search allowClear placeholder="搜索数据名称或数据 Id" style={{ width: 240 }} onSearch={(v) => (setQ(v), setPage(1))} />}
      >
        <Table<DataRow>
          size="small"
          rowKey="id"
          loading={list.isLoading}
          dataSource={list.data?.items ?? []}
          locale={{ emptyText: <Empty description={empty} /> }}
          onRow={(r) => ({ onClick: () => router.push(`/grants/data/${r.id}`), style: { cursor: "pointer" } })}
          pagination={{ current: page, pageSize: 50, total: list.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
          columns={[
            {
              title: "数据",
              render: (_, r) => (
                <div>
                  <b>{r.data_name ?? r.data_id}</b>
                  <div className="uc-mono uc-muted">{r.data_id}</div>
                </div>
              ),
            },
            { title: "数据编码", render: (_, r) => <Space><Tag className="uc-mono">{r.data_code}</Tag><span className="uc-muted">{r.app}</span></Space> },
            { title: "我的权限", render: (_, r) => <LevelTag level={r.my_level} /> },
            { title: "Owner", render: (_, r) => (r.owners.length ? r.owners.join("、") : <span style={{ color: "#dc2626" }}>无可用 Owner</span>) },
            { title: "授权", render: (_, r) => <span className="uc-muted">{r.user_grants} 个用户 · {r.dept_grants} 个组织</span> },
            { title: "创建时间", render: (_, r) => <span className="uc-muted">{fmtTime(r.created_at)}</span> },
          ]}
        />
      </Card>
    </>
  );
}

// ───────────────────────────────────────────── 岗位管理

function PositionsTab() {
  const can = useCan();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const list = usePositions();
  const [form, setForm] = useState<{ open: boolean; pos?: PositionRow | null }>({ open: false });
  const [grantTo, setGrantTo] = useState<PositionRow | null>(null);
  const del = useMutation({
    mutationFn: (id: string) => api.del(`/positions/${id}`),
    onSuccess: () => (qc.invalidateQueries({ queryKey: ["positions"] }), message.success("已删除")),
    onError: (e) => message.error(errorText(e)),
  });
  return (
    <Card
      size="small"
      extra={can("position:manage") && <Button type="primary" onClick={() => setForm({ open: true, pos: null })}>＋ 新建岗位</Button>}
    >
      <Table<PositionRow>
        size="small"
        rowKey="id"
        loading={list.isLoading}
        dataSource={list.data ?? []}
        pagination={false}
        columns={[
          { title: "岗位", render: (_, p) => <b>{p.name}</b> },
          { title: "编码", render: (_, p) => <span className="uc-mono">{p.code}</span> },
          { title: "默认角色", render: (_, p) => p.roles.map((r) => <Tag color="purple" key={r.id}>{r.name} · {r.app}</Tag>) },
          {
            title: "授权对象",
            render: (_, p) =>
              p.grants.length ? (
                <span>
                  {p.grants.slice(0, 3).map((g) => (
                    <Tag key={g.id}>
                      {g.subject.type === "user" ? "👤" : "🏢"} {g.subject.name}
                      {g.include_sub ? "（含下级）" : ""}
                    </Tag>
                  ))}
                  {p.grants.length > 3 && <span className="uc-muted">等 {p.grants.length} 个</span>}
                </span>
              ) : (
                <span className="uc-muted">尚未授权</span>
              ),
          },
          { title: "人数", dataIndex: "holders" },
          {
            title: "",
            align: "right",
            render: (_, p) => (
              <Space size={0}>
                {can("grant:manage") && <Button type="link" size="small" onClick={() => setGrantTo(p)}>授权</Button>}
                {can("position:manage") && <Button type="link" size="small" onClick={() => setForm({ open: true, pos: p })}>编辑</Button>}
                {can("position:manage") && (
                  <Button
                    type="link"
                    size="small"
                    danger
                    onClick={() => modal.confirm({ title: `删除岗位 · ${p.name}`, okButtonProps: { danger: true }, onOk: () => del.mutateAsync(p.id) })}
                  >
                    删除
                  </Button>
                )}
              </Space>
            ),
          },
        ]}
      />
      {form.open && <PositionForm pos={form.pos ?? null} onClose={() => setForm({ open: false })} />}
      {grantTo && <GrantToModal kind="position" target={grantTo} onClose={() => setGrantTo(null)} />}
    </Card>
  );
}

function PositionForm({ pos, onClose }: { pos: PositionRow | null; onClose: () => void }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [roles, setRoles] = useState<string[]>(pos?.roles.map((r) => r.id) ?? []);
  const [form] = Form.useForm();
  async function submit(v: { code: string; name: string }) {
    try {
      if (pos) await api.patch(`/positions/${pos.id}`, { name: v.name, role_ids: roles, version: pos.version });
      else await api.post("/positions", { code: v.code, name: v.name, role_ids: roles });
      qc.invalidateQueries({ queryKey: ["positions"] });
      message.success("岗位已保存");
      onClose();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open title={pos ? `编辑岗位 · ${pos.name}` : "新建岗位"} onCancel={onClose} onOk={() => form.submit()} okText="保存" width={600}>
      <Form form={form} layout="vertical" onFinish={submit} initialValues={pos ? { name: pos.name, code: pos.code } : {}}>
        <Space style={{ width: "100%" }} align="start">
          <Form.Item name="name" label="岗位名称" rules={[{ required: true }]} style={{ width: 260 }}>
            <Input maxLength={32} />
          </Form.Item>
          <Form.Item name="code" label="岗位编码" rules={[{ required: true, pattern: /^[A-Z][A-Z0-9_]{1,31}$/, message: "大写字母开头，大写字母、数字、下划线" }]} style={{ width: 260 }}>
            <Input disabled={!!pos} placeholder="如 BACKEND_ENGINEER" />
          </Form.Item>
        </Space>
        <div style={{ marginBottom: 6 }}>默认角色</div>
        <div className="uc-muted" style={{ marginBottom: 8 }}>
          获得该岗位的人（含通过部门获得）自动拥有以下角色；调整后立即生效
        </div>
        <RoleChecks value={roles} onChange={setRoles} />
      </Form>
    </Modal>
  );
}
