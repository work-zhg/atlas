"use client";

/**
 * 数据权限：个人中心「我的数据」（我管理的 / 我可访问的）与「权限 › 数据」（全部，只读）共用。
 * 点一行在右侧抽屉里看授权、改授权，不离开列表（原来是跳独立页面）。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Drawer, Empty, Input, Modal, Radio, Segmented, Select, Space, Table, Tag } from "antd";
import { useState } from "react";

import { LevelTag, PersonCell, SquareIcon, SubjectPicker, toSubjects, type SubjectValue } from "@/components/common";
import { Icon } from "@/components/Icon";
import { api, errorText } from "@/lib/api";
import { fmtTime, LEVEL } from "@/lib/format";
import type { DataRow, DataTypeInfo, Level, Page } from "@/lib/types";

interface Source {
  type: "USER" | "DEPT";
  dept?: string;
  include_sub?: boolean;
  level: Level;
  /** data = 授给这条数据；data_type = 授给它所属编码下的全部数据 */
  scope?: "data" | "data_type";
}

interface DataDetail {
  id: string;
  data_code: string;
  data_type_name: string;
  app: string;
  data_id: string;
  data_name: string | null;
  created_at: string;
  my_level: Level;
  my_sources: Source[];
  is_owner: boolean;
  can_manage: boolean;
  has_active_owner: boolean;
  acl: {
    id: string;
    level: Level;
    subject: { type: "user" | "dept"; id: string; name: string; account?: string; status?: string };
    include_sub: boolean;
    source: "api" | "manual";
    granted_by: string | null;
    granted_app: string | null;
    granted_at: string;
  }[];
  holders: { user: { id: string; name: string; account: string }; dept: string; level: Level; sources: Source[] }[];
}

const srcText = (s: Source) =>
  `${s.scope === "data_type" ? "全部数据授权·" : ""}${s.type === "USER" ? "直接授权" : `部门：${s.dept}${s.include_sub ? "（含下级）" : ""}`}·${LEVEL[s.level].label}`;
const LEVEL_OPTS = (["READ", "WRITE", "OWNER"] as const).map((l) => ({ value: l, label: LEVEL[l].label }));

/** where=me：我管理的 / 我可访问的；where=all：全部数据（需要 data:view，只读） */
export function DataList({ where }: { where: "me" | "all" }) {
  const [scope, setScope] = useState(where === "all" ? "all" : "mine");
  const [code, setCode] = useState<string>();
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<string>();
  const types = useQuery({ queryKey: ["data-types"], queryFn: () => api.get<DataTypeInfo[]>("/data-types") });
  const list = useQuery({
    queryKey: ["data", scope, code, q, page],
    queryFn: () => api.get<Page<DataRow> & { counts: Record<string, number> }>("/data", { scope, data_code: code, q, page, size: 50 }),
  });
  const counts = list.data?.counts ?? {};
  const empty = {
    mine: "你还不是任何数据的 Owner。在应用里创建智能体、知识库时，你会自动成为它的 Owner。",
    access: "还没有人把数据授权给你或你所在的部门。",
    all: "暂无数据",
  }[scope];
  return (
    <>
      <div className="uc-toolbar">
        {where === "me" && (
          <Segmented
            value={scope}
            onChange={(v) => (setScope(String(v)), setPage(1))}
            options={[
              { value: "mine", label: <>我管理的 <span className="uc-sub">{counts.mine ?? 0}</span></> },
              { value: "access", label: <>我可访问的 <span className="uc-sub">{counts.access ?? 0}</span></> },
            ]}
          />
        )}
        <Select
          allowClear
          placeholder="全部数据类型"
          style={{ width: 220 }}
          value={code}
          onChange={(v) => (setCode(v), setPage(1))}
          options={(types.data ?? []).map((t) => ({ value: t.code, label: `${t.code} · ${t.name}` }))}
        />
        <Input.Search allowClear placeholder="搜索数据名称或 Id" style={{ width: 240 }} onSearch={(v) => (setQ(v), setPage(1))} />
      </div>
      <Card styles={{ body: { padding: 0 } }}>
        <Table<DataRow>
          rowKey="id"
          loading={list.isLoading}
          dataSource={list.data?.items ?? []}
          locale={{ emptyText: <Empty description={empty} /> }}
          onRow={(r) => ({ onClick: () => setOpen(r.id), className: "uc-row-click" })}
          pagination={{ current: page, pageSize: 50, total: list.data?.total ?? 0, onChange: setPage, showSizeChanger: false, hideOnSinglePage: true }}
          columns={[
            {
              title: "数据",
              render: (_, r) => (
                <>
                  <b>{r.data_name ?? r.data_id}</b>
                  <div className="uc-sub uc-mono">{r.data_id}</div>
                </>
              ),
            },
            {
              title: "类型",
              render: (_, r) => (
                <span className="uc-row">
                  <Tag className="uc-mono" style={{ marginInlineEnd: 0 }}>
                    {r.data_code}
                  </Tag>
                  <span className="uc-muted uc-nw">{r.app}</span>
                </span>
              ),
            },
            ...(where === "me" ? [{ title: "我的权限", render: (_: unknown, r: DataRow) => <LevelTag level={r.my_level} /> }] : []),
            { title: "Owner", render: (_, r) => (r.owners.length ? r.owners.join("、") : <span style={{ color: "var(--danger)" }}>无可用 Owner</span>) },
            { title: "授权", render: (_, r) => <span className="uc-muted uc-nw">{r.user_grants} 个人 · {r.dept_grants} 个组织</span> },
            { title: "创建", render: (_, r) => <span className="uc-muted uc-nw">{fmtTime(r.created_at)}</span> },
          ]}
        />
      </Card>
      <Drawer open={!!open} width={640} onClose={() => setOpen(undefined)} destroyOnHidden title={null} closable={false} styles={{ body: { padding: 0 } }}>
        {open && <DataDetailView id={open} onClose={() => setOpen(undefined)} />}
      </Drawer>
    </>
  );
}

function DataDetailView({ id, onClose }: { id: string; onClose: () => void }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [adding, setAdding] = useState(false);
  const q = useQuery({ queryKey: ["data", "detail", id], queryFn: () => api.get<DataDetail>(`/data/${id}`) });
  const d = q.data;
  const refresh = () => qc.invalidateQueries({ queryKey: ["data"] });

  async function act(fn: () => Promise<unknown>, ok: string) {
    try {
      await fn();
      refresh();
      message.success(ok);
    } catch (e) {
      message.error(errorText(e));
      refresh();
    }
  }

  if (!d) return <Card loading={q.isLoading}>{q.error ? errorText(q.error) : null}</Card>;
  return (
    <>
      <div className="uc-row" style={{ gap: 10, padding: "16px 20px", borderBottom: "1px solid var(--line)" }}>
        <SquareIcon icon="database" size={36} />
        <div className="uc-grow">
          <h2 style={{ margin: 0, fontSize: 17 }}>{d.data_name ?? d.data_id}</h2>
          <div className="uc-sub">
            <span className="uc-mono">{d.data_code}</span> · {d.data_type_name} · {d.app} · <span className="uc-mono">{d.data_id}</span>
          </div>
        </div>
        <Button type="text" aria-label="关闭" icon={<Icon name="x" />} onClick={onClose} />
      </div>
      <div style={{ padding: "16px 20px" }}>
        <div className="uc-row" style={{ marginBottom: 12 }}>
          <span className="uc-muted">我的权限</span>
          <LevelTag level={d.my_level} />
          {d.my_sources.length > 0 && <span className="uc-sub">来自：{d.my_sources.map(srcText).join("、")}</span>}
        </div>
        {!d.can_manage && <Alert style={{ marginBottom: 12 }} type="info" showIcon message="只有 Owner 可以修改这条数据的授权。" />}
        {d.can_manage && !d.is_owner && <Alert style={{ marginBottom: 12 }} type="info" showIcon message={`你以「全部${d.data_type_name} · Owner」（数据编码级授权）身份管理这条数据的授权。`} />}
        {!d.has_active_owner && <Alert style={{ marginBottom: 12 }} type="warning" showIcon message="这条数据目前没有可用的 Owner（Owner 已停用或调离）。" />}

        <div className="uc-row" style={{ margin: "4px 0 8px" }}>
          <b>授权</b>
          <span className="uc-sub">{d.acl.length} 条</span>
          <div className="uc-grow" />
          {d.can_manage && (
            <Button size="small" type="primary" icon={<Icon name="plus" size={14} />} onClick={() => setAdding(true)}>
              添加授权
            </Button>
          )}
        </div>
        <Card styles={{ body: { padding: 0 } }}>
          <Table
            size="small"
            rowKey="id"
            pagination={false}
            dataSource={d.acl}
            columns={[
              {
                title: "对象",
                render: (_, a) => (
                  <span className="uc-row">
                    <Icon name={a.subject.type === "user" ? "user" : "building"} size={14} />
                    <b>{a.subject.name}</b>
                    {a.subject.type === "dept" && <span className="uc-sub">{a.include_sub ? "含下级" : "仅本部门"}</span>}
                    {a.subject.status === "disabled" && <Tag color="warning">已停用</Tag>}
                  </span>
                ),
              },
              {
                title: "权限",
                render: (_, a) =>
                  d.can_manage ? (
                    <Select
                      size="small"
                      value={a.level}
                      style={{ width: 96 }}
                      options={LEVEL_OPTS}
                      onChange={(v) => act(() => api.patch(`/data-acl/${a.id}`, { level: v }), `已改为「${LEVEL[v as Level].label}」`)}
                    />
                  ) : (
                    <LevelTag level={a.level} />
                  ),
              },
              { title: "来源", render: (_, a) => <span className="uc-sub">{a.source === "api" ? `接口写入 · ${a.granted_app}` : `${a.granted_by ?? "—"} · ${fmtTime(a.granted_at)}`}</span> },
              {
                title: "",
                align: "right",
                render: (_, a) =>
                  d.can_manage && (
                    <span className="uc-nw">
                      {a.subject.type === "dept" && (
                        <Button type="text" size="small" onClick={() => act(() => api.patch(`/data-acl/${a.id}`, { include_sub: !a.include_sub }), "范围已调整")}>
                          {a.include_sub ? "改为仅本部门" : "改为含下级"}
                        </Button>
                      )}
                      <Button type="text" size="small" danger onClick={() => act(() => api.del(`/data-acl/${a.id}`), "已移除")}>
                        移除
                      </Button>
                    </span>
                  ),
              },
            ]}
          />
        </Card>
        {d.can_manage && <div className="uc-sub" style={{ marginTop: 6 }}>至少保留一个 Owner；Owner 能管理这条数据的授权。</div>}

        <div className="uc-row" style={{ margin: "20px 0 8px" }}>
          <b>实际可访问的人</b>
          <span className="uc-sub">{d.holders.length} 人 · 按授权自动计算，多个来源取最高级别</span>
        </div>
        <Card styles={{ body: { padding: 0 } }}>
          <Table
            size="small"
            rowKey={(h) => h.user.id}
            pagination={false}
            dataSource={d.holders}
            columns={[
              { title: "人", render: (_, h) => <PersonCell name={h.user.name} account={h.dept} /> },
              { title: "权限", render: (_, h) => <LevelTag level={h.level} /> },
              { title: "来源", render: (_, h) => <span className="uc-sub">{h.sources.map(srcText).join("、")}</span> },
            ]}
          />
        </Card>
      </div>
      {adding && <AddAcl id={d.id} name={d.data_name ?? d.data_id} onClose={() => setAdding(false)} onDone={refresh} />}
    </>
  );
}

function AddAcl({ id, name, onClose, onDone }: { id: string; name: string; onClose: () => void; onDone: () => void }) {
  const { message } = App.useApp();
  const [level, setLevel] = useState<"READ" | "WRITE" | "OWNER">("READ");
  const [value, setValue] = useState<SubjectValue>({ depts: [], users: [], includeSub: true });
  async function ok() {
    const subjects = toSubjects(value);
    if (!subjects.length) return message.warning("请选择组织或用户");
    try {
      const r = await api.post<{ added: number; updated: number }>(`/data/${id}/acl`, { level, subjects, include_sub: value.includeSub });
      onDone();
      message.success(`新增 ${r.added} 条${r.updated ? `，更新 ${r.updated} 条` : ""}`);
      onClose();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open title={`添加授权 · ${name}`} onCancel={onClose} onOk={ok} okText="授权" width={560}>
      <Space direction="vertical" style={{ width: "100%" }} size={12}>
        <div>
          <div style={{ marginBottom: 6, fontWeight: 600 }}>权限</div>
          <Radio.Group value={level} onChange={(e) => setLevel(e.target.value)} options={LEVEL_OPTS} />
          <div className="uc-sub" style={{ marginTop: 4 }}>
            授予 Owner 后，对方也能管理这条数据的授权
          </div>
        </div>
        <SubjectPicker digital value={value} onChange={setValue} />
        <div className="uc-sub">对象已有授权时，按本次选择更新级别（和部门范围）。</div>
      </Space>
    </Modal>
  );
}
