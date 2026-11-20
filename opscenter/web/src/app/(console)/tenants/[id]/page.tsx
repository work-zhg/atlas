"use client";

/**
 * 一个租户：基本信息（以用户中心为准）、开通的应用、各系统的份额和用量（只有数字）、做过的操作和进度。
 * 停用先挡用户中心的入口再挂起各系统；关闭只能对已停用的做，要输入企业代码确认，不可恢复。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Space,
  Switch,
  Table,
  Tag,
  Timeline,
  Typography,
} from "antd";
import { useParams } from "next/navigation";
import { useState } from "react";

import { ErrorBox, OneTimeSecret, PageHead, errorText } from "@/components/common";
import { UC, api, sys } from "@/lib/api";
import { ACTION_LABEL, GB, KIND_LABEL, TENANT_STATUS, bytes, dateTime, limit } from "@/lib/format";
import { systemName, useMe, useSystems, withCapability } from "@/lib/session";
import type { Operation, SystemInfo, Tenant, TenantApp, TenantUsage } from "@/lib/types";

interface Detail {
  tenant: Tenant;
  operations: Operation[];
}

export default function TenantPage() {
  const { id } = useParams<{ id: string }>();
  const { data: me } = useMe();
  const admin = me?.role === "ADMIN";
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const q = useQuery({
    queryKey: ["tenant", id],
    queryFn: () => api.get<Detail>(`/tenants/${id}`),
    refetchInterval: (query) => (query.state.data?.operations.some((o) => o.status === "RUNNING") ? 3000 : 15000),
  });
  const { data: systems } = useSystems();
  const [edit, setEdit] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const t = q.data?.tenant;
  const running = q.data?.operations.find((o) => o.status === "RUNNING");

  const refresh = () => Promise.all([qc.invalidateQueries({ queryKey: ["tenant", id] }), qc.invalidateQueries({ queryKey: ["tenants"] })]);

  async function run(fn: () => Promise<unknown>, ok: string) {
    try {
      await fn();
      message.success(ok);
      await refresh();
    } catch (e) {
      message.error(errorText(e));
    }
  }

  function suspend() {
    let reason = "";
    modal.confirm({
      title: `停用 ${t?.name}`,
      content: (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Typography.Text>先挡住用户中心的入口（登录、各系统调用都会被拒），再挂起各系统：在跑的 run 挂起、沙箱回收。数据都留着，恢复后接着用。</Typography.Text>
          <Input placeholder="原因（可选）" onChange={(e) => (reason = e.target.value)} />
        </Space>
      ),
      okText: "停用",
      okButtonProps: { danger: true },
      onOk: () => run(() => api.post(`/tenants/${id}/suspend`, { reason: reason || null }), "已发起停用"),
    });
  }

  function close() {
    let code = "";
    modal.confirm({
      title: `关闭并删除 ${t?.name}`,
      content: (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Typography.Text type="danger">
            各系统会删除这个租户的全部数据（会话、文件、知识库、技能、模型、MCP、记忆……），最后删除用户中心里的租户。不可恢复。
          </Typography.Text>
          <Typography.Text>输入企业代码 <b>{t?.code}</b> 确认：</Typography.Text>
          <Input onChange={(e) => (code = e.target.value)} />
        </Space>
      ),
      okText: "关闭并删除",
      okButtonProps: { danger: true },
      onOk: () => run(() => api.post(`/tenants/${id}/close`, { confirm_code: code }), "已发起关闭"),
    });
  }

  async function reissue() {
    try {
      const out = await api.post<{ temp_password: string }>(`/tenants/${id}/admin-password`);
      setSecret(out.temp_password);
    } catch (e) {
      message.error(errorText(e));
    }
  }

  if (q.error) return <ErrorBox error={q.error} />;
  if (!t) return null;
  const status = TENANT_STATUS[t.status] ?? { text: t.status, color: "default" };

  return (
    <>
      <PageHead
        title={`${t.name}（${t.code}）`}
        sub="租户本身以用户中心为准；运营中心看不到租户里的人、智能体、会话这些内容"
        extra={
          admin && (
            <Space>
              <Button onClick={() => setEdit(true)}>改名字 / 名额 / 到期</Button>
              <Button onClick={reissue}>补发首个管理员的临时密码</Button>
              {t.status === "active" ? (
                <Button danger disabled={!!running} onClick={suspend}>
                  停用
                </Button>
              ) : (
                <>
                  <Button disabled={!!running} onClick={() => run(() => api.post(`/tenants/${id}/resume`), "已发起恢复")}>
                    恢复
                  </Button>
                  <Button danger disabled={!!running} onClick={close}>
                    关闭并删除
                  </Button>
                </>
              )}
            </Space>
          )
        }
      />
      {secret && (
        <div style={{ marginBottom: 16 }}>
          <OneTimeSecret label="首个管理员的新临时密码" value={secret} />
        </div>
      )}
      <Card style={{ marginBottom: 16 }}>
        <Descriptions column={3} size="small">
          <Descriptions.Item label="状态">
            <Tag color={status.color}>{status.text}</Tag>
            {t.status === "active" && !t.usable && <Tag color="red">已到期</Tag>}
          </Descriptions.Item>
          <Descriptions.Item label="人数 / 名额">
            {t.users ?? 0} / {limit(t.max_users)}
          </Descriptions.Item>
          <Descriptions.Item label="到期">{t.expires_at ? dateTime(t.expires_at) : "不到期"}</Descriptions.Item>
          <Descriptions.Item label="开通时间">{dateTime(t.created_at)}</Descriptions.Item>
          <Descriptions.Item label="租户 id">
            <Typography.Text copyable style={{ fontFamily: "monospace", fontSize: 12 }}>
              {t.id}
            </Typography.Text>
          </Descriptions.Item>
        </Descriptions>
      </Card>
      <Space align="start" style={{ width: "100%" }} styles={{ item: { flex: 1, minWidth: 0 } }}>
        <TenantApps tenantId={id} admin={admin} />
        <TenantQuotas tenantId={id} admin={admin} systems={withCapability(systems, "capacity")} />
      </Space>
      <Card title="做过的操作" style={{ marginTop: 16 }}>
        <Operations ops={q.data?.operations ?? []} systems={systems} admin={admin} onChanged={refresh} />
      </Card>
      {edit && <EditTenant tenant={t} onClose={() => setEdit(false)} onSaved={refresh} />}
    </>
  );
}

function TenantApps({ tenantId, admin }: { tenantId: string; admin: boolean }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const q = useQuery({
    queryKey: ["tenant-apps", tenantId],
    queryFn: () => sys(UC).get<TenantApp[]>(`/tenants/${tenantId}/apps`),
  });
  async function toggle(appId: string, enabled: boolean) {
    try {
      await sys(UC).put(`/tenants/${tenantId}/apps/${appId}`, { enabled });
      await qc.invalidateQueries({ queryKey: ["tenant-apps", tenantId] });
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Card title="开通的应用" size="small">
      <ErrorBox error={q.error} />
      <Table<TenantApp>
        rowKey={(r) => r.app.id}
        size="small"
        pagination={false}
        loading={q.isLoading}
        dataSource={q.data ?? []}
        columns={[
          { title: "应用", render: (_, r) => `${r.app.icon ?? ""} ${r.app.name}` },
          { title: "编码", render: (_, r) => r.app.code ?? "—" },
          {
            title: "开通",
            render: (_, r) => <Switch size="small" checked={r.enabled} disabled={!admin} onChange={(on) => toggle(r.app.id, on)} />,
          },
        ]}
      />
    </Card>
  );
}

function TenantQuotas({ tenantId, admin, systems }: { tenantId: string; admin: boolean; systems: SystemInfo[] }) {
  return (
    <Card title="份额和用量" size="small">
      {systems.map((s) => (
        <QuotaRow key={s.code} system={s} tenantId={tenantId} admin={admin} />
      ))}
    </Card>
  );
}

const QUOTA_LABEL: Record<string, string> = { running_runs: "同时运行的 run", sandboxes: "沙箱 / 会话 Pod", storage_bytes: "存储" };

function fmt(field: string, v: number | null | undefined): string {
  return field === "storage_bytes" ? bytes(v) : limit(v);
}

function QuotaRow({ system, tenantId, admin }: { system: SystemInfo; tenantId: string; admin: boolean }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const q = useQuery({
    queryKey: ["usage", system.code, tenantId],
    queryFn: () => sys(system.code).get<TenantUsage>(`/usage/${tenantId}`),
  });
  const [editing, setEditing] = useState<Record<string, number | null> | null>(null);
  const fields = Object.keys(q.data?.quota ?? {});

  async function save(body: Record<string, unknown>) {
    try {
      await sys(system.code).put(`/quotas/${tenantId}`, body);
      setEditing(null);
      await qc.invalidateQueries({ queryKey: ["usage", system.code, tenantId] });
      message.success("份额已保存");
    } catch (e) {
      message.error(errorText(e));
    }
  }

  return (
    <div style={{ marginBottom: 12 }}>
      <Space style={{ width: "100%", justifyContent: "space-between" }}>
        <Typography.Text strong>
          {system.name} {q.data?.is_default && <Tag>默认份额</Tag>}
        </Typography.Text>
        {admin && q.data && (
          <Space size={4}>
            {!q.data.is_default && (
              <Button size="small" type="link" onClick={() => save({ use_default: true })}>
                回到默认份额
              </Button>
            )}
            <Button
              size="small"
              type="link"
              onClick={() =>
                setEditing(
                  Object.fromEntries(
                    fields.map((f) => {
                      const v = q.data?.quota[f as keyof typeof q.data.quota] ?? null;
                      return [f, f === "storage_bytes" && v !== null ? v / GB : v];
                    }),
                  ),
                )
              }
            >
              单独定份额
            </Button>
          </Space>
        )}
      </Space>
      <ErrorBox error={q.error} />
      {q.data &&
        fields.map((f) => (
          <div key={f} style={{ display: "flex", justifyContent: "space-between", fontSize: 13, padding: "2px 0" }}>
            <Typography.Text type="secondary">{QUOTA_LABEL[f] ?? f}</Typography.Text>
            <span>
              {fmt(f, q.data?.usage[f as keyof TenantUsage["usage"]] ?? 0)} / {fmt(f, q.data?.quota[f as keyof TenantUsage["quota"]])}
            </span>
          </div>
        ))}
      {editing && (
        <Modal
          open
          title={`${system.name} · 单独定份额`}
          onCancel={() => setEditing(null)}
          onOk={() =>
            save(
              Object.fromEntries(
                Object.entries(editing).map(([k, v]) => [k, v === null ? null : k === "storage_bytes" ? Math.round(v * GB) : v]),
              ),
            )
          }
        >
          <Space direction="vertical">
            {Object.entries(editing).map(([k, v]) => (
              <Space key={k}>
                <span style={{ width: 120, display: "inline-block" }}>{QUOTA_LABEL[k] ?? k}</span>
                <InputNumber
                  min={0}
                  placeholder="不限"
                  value={v}
                  addonAfter={k === "storage_bytes" ? "GB" : undefined}
                  onChange={(n) => setEditing({ ...editing, [k]: (n as number | null) ?? null })}
                />
              </Space>
            ))}
            <Typography.Text type="secondary">留空 = 不限</Typography.Text>
          </Space>
        </Modal>
      )}
    </div>
  );
}

function Operations({
  ops,
  systems,
  admin,
  onChanged,
}: {
  ops: Operation[];
  systems: SystemInfo[] | undefined;
  admin: boolean;
  onChanged: () => Promise<unknown>;
}) {
  const { message } = App.useApp();
  if (!ops.length) return <Typography.Text type="secondary">还没有操作</Typography.Text>;
  return (
    <Space direction="vertical" style={{ width: "100%" }} size={16}>
      {ops.map((op) => (
        <div key={op.id}>
          <Space>
            <Tag color={op.status === "DONE" ? "green" : "processing"}>{KIND_LABEL[op.kind]}</Tag>
            <Typography.Text>{op.status === "DONE" ? `完成于 ${dateTime(op.finished_at)}` : "进行中"}</Typography.Text>
            <Typography.Text type="secondary">
              {op.requested_by ?? "—"} · {dateTime(op.created_at)}
              {op.reason ? ` · ${op.reason}` : ""}
            </Typography.Text>
            {admin && op.stuck && op.status === "RUNNING" && (
              <Button
                size="small"
                onClick={async () => {
                  try {
                    await api.post(`/operations/${op.id}/retry`);
                    message.success("马上重做失败的步骤");
                    await onChanged();
                  } catch (e) {
                    message.error(errorText(e));
                  }
                }}
              >
                立即重做
              </Button>
            )}
          </Space>
          <Timeline
            style={{ marginTop: 12, marginBottom: 0 }}
            items={op.steps.map((s) => ({
              color: s.status === "DONE" ? "green" : s.status === "FAILED" ? (s.rejected ? "red" : "orange") : "gray",
              children: (
                <div>
                  <Typography.Text>
                    {systemName(systems, s.system)} · {ACTION_LABEL[s.action] ?? s.action}
                  </Typography.Text>{" "}
                  <Typography.Text type="secondary">
                    {s.status === "DONE"
                      ? `完成 ${dateTime(s.finished_at)}`
                      : s.status === "FAILED"
                        ? `第 ${s.attempts} 次失败，${dateTime(s.next_attempt_at)} 再试`
                        : s.attempts
                          ? `进行中（已做 ${s.attempts} 批）`
                          : "等待"}
                  </Typography.Text>
                  {s.last_error && (
                    <div>
                      <Typography.Text type={s.rejected ? "danger" : "warning"} style={{ fontSize: 12 }}>
                        {s.rejected ? "对方拒绝：" : ""}
                        {s.last_error}
                      </Typography.Text>
                    </div>
                  )}
                  {s.result?.deleted !== undefined && (
                    <div style={{ fontSize: 12, color: "#888" }}>
                      删了 {Object.entries(s.result.deleted as Record<string, number>).map(([k, n]) => `${k} ${n}`).join("、") || "0"}
                    </div>
                  )}
                </div>
              ),
            }))}
          />
        </div>
      ))}
    </Space>
  );
}

function EditTenant({ tenant, onClose, onSaved }: { tenant: Tenant; onClose: () => void; onSaved: () => Promise<unknown> }) {
  const [form] = Form.useForm();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(v: { name: string; max_users?: number | null; expires_on?: string }) {
    setBusy(true);
    try {
      await api.patch(`/tenants/${tenant.id}`, {
        name: v.name,
        max_users: v.max_users || null,
        expires_at: v.expires_on ? new Date(`${v.expires_on}T23:59:59`).toISOString() : null,
      });
      await onSaved();
      onClose();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open title="改名字 / 名额 / 到期" onCancel={onClose} onOk={() => form.submit()} confirmLoading={busy}>
      <ErrorBox error={error} />
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          name: tenant.name,
          max_users: tenant.max_users,
          expires_on: tenant.expires_at ? tenant.expires_at.slice(0, 10) : undefined,
        }}
        onFinish={submit}
      >
        <Form.Item name="name" label="名称" rules={[{ required: true }]}>
          <Input />
        </Form.Item>
        <Form.Item name="max_users" label="人员名额" extra="不填 = 不限">
          <InputNumber min={1} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="expires_on" label="到期日" extra="不填 = 不到期；到期后自动停用">
          <Input type="date" />
        </Form.Item>
      </Form>
    </Modal>
  );
}
