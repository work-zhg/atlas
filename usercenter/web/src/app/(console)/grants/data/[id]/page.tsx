"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Modal, Radio, Select, Space, Table, Tag } from "antd";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { LevelTag, SubjectPicker, toSubjects, type SubjectValue } from "@/components/common";
import { api, errorText } from "@/lib/api";
import { fmtTime, LEVEL } from "@/lib/format";
import type { Level } from "@/lib/types";

interface Source {
  type: "USER" | "DEPT";
  dept?: string;
  include_sub?: boolean;
  level: Level;
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

const srcText = (s: Source) => `${s.type === "USER" ? "直接授权" : `部门：${s.dept}${s.include_sub ? "（含下级）" : ""}`}·${LEVEL[s.level].label}`;
const LEVEL_OPTS = (["READ", "WRITE", "OWNER"] as const).map((l) => ({ value: l, label: LEVEL[l].label }));

export default function DataDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
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
      <div style={{ display: "flex", marginBottom: 12 }}>
        <div className="uc-grow" />
        <Button onClick={() => router.push("/grants?tab=data")}>← 返回数据授权</Button>
      </div>
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        <Card>
          <div style={{ display: "flex", gap: 12 }}>
            <div>
              <Space>
                <Tag className="uc-mono">{d.data_code}</Tag>
                <span className="uc-muted">
                  {d.data_type_name} · {d.app}
                </span>
              </Space>
              <h2 style={{ margin: "6px 0 0", fontSize: 20 }}>{d.data_name ?? d.data_id}</h2>
              <div className="uc-muted">
                数据 Id <span className="uc-mono">{d.data_id}</span> · 创建于 {fmtTime(d.created_at)}
              </div>
            </div>
            <div className="uc-grow" />
            <div style={{ textAlign: "right" }}>
              <div className="uc-muted">我的权限</div>
              <LevelTag level={d.my_level} />
              {d.my_sources.length > 0 && <div className="uc-muted">来自：{d.my_sources.map(srcText).join("、")}</div>}
            </div>
          </div>
          {!d.can_manage && <Alert style={{ marginTop: 12 }} type="info" showIcon message="只有 Owner 可以修改这条数据的授权。" />}
          {d.can_manage && !d.is_owner && <Alert style={{ marginTop: 12 }} type="info" showIcon message="你以数据管理员身份管理这条数据的授权。" />}
          {!d.has_active_owner && <Alert style={{ marginTop: 12 }} type="warning" showIcon message="这条数据目前没有可用的 Owner（Owner 已停用或调离）。" />}
        </Card>
        <Card
          size="small"
          title={`授权（${d.acl.length}）`}
          extra={d.can_manage && <Button type="primary" size="small" onClick={() => setAdding(true)}>＋ 添加授权</Button>}
        >
          <Table
            size="small"
            rowKey="id"
            pagination={false}
            dataSource={d.acl}
            columns={[
              {
                title: "授权对象",
                render: (_, a) =>
                  a.subject.type === "user" ? (
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
                  d.can_manage ? (
                    <Select
                      size="small"
                      value={a.level}
                      style={{ width: 100 }}
                      options={LEVEL_OPTS}
                      onChange={(v) => act(() => api.patch(`/data-acl/${a.id}`, { level: v }), `已改为「${LEVEL[v as Level].label}」`)}
                    />
                  ) : (
                    <LevelTag level={a.level} />
                  ),
              },
              { title: "范围", render: (_, a) => (a.subject.type === "user" ? "本人" : a.include_sub ? "本部门及下级部门" : "仅本部门") },
              { title: "来源", render: (_, a) => <span className="uc-muted">{a.source === "api" ? `接口写入 · ${a.granted_app}` : `${a.granted_by ?? "—"} 授权`}</span> },
              { title: "时间", render: (_, a) => <span className="uc-muted">{fmtTime(a.granted_at)}</span> },
              {
                title: "",
                align: "right",
                render: (_, a) =>
                  d.can_manage && (
                    <Space size={0}>
                      {a.subject.type === "dept" && (
                        <Button type="link" size="small" onClick={() => act(() => api.patch(`/data-acl/${a.id}`, { include_sub: !a.include_sub }), "范围已调整")}>
                          {a.include_sub ? "改为仅本部门" : "改为含下级"}
                        </Button>
                      )}
                      <Button type="link" size="small" danger onClick={() => act(() => api.del(`/data-acl/${a.id}`), "已移除")}>
                        移除
                      </Button>
                    </Space>
                  ),
              },
            ]}
          />
        </Card>
        <Card size="small" title={`实际可访问的人（${d.holders.length}）`} extra={<span className="uc-muted">按授权自动计算，多个来源取最高级别</span>}>
          <Table
            size="small"
            rowKey={(h) => h.user.id}
            pagination={false}
            dataSource={d.holders}
            columns={[
              { title: "用户", render: (_, h) => <b>{h.user.name}</b> },
              { title: "部门", dataIndex: "dept" },
              { title: "有效权限", render: (_, h) => <LevelTag level={h.level} /> },
              { title: "来源", render: (_, h) => <span className="uc-muted">{h.sources.map(srcText).join("、")}</span> },
            ]}
          />
        </Card>
      </Space>
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
      <div style={{ marginBottom: 12 }}>
        <div style={{ marginBottom: 6 }}>权限</div>
        <Radio.Group value={level} onChange={(e) => setLevel(e.target.value)} options={LEVEL_OPTS} />
        <div className="uc-muted" style={{ marginTop: 4 }}>
          授予 Owner 后，对方也能管理这条数据的授权
        </div>
      </div>
      <SubjectPicker value={value} onChange={setValue} />
      <div className="uc-muted" style={{ marginTop: 8 }}>
        对象已有授权时，按本次选择更新级别（和部门范围）。
      </div>
    </Modal>
  );
}
