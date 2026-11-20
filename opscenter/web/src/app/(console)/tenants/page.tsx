"use client";

/**
 * 租户：名单、状态以用户中心为准；运营中心只记「对租户做过什么、做到哪了」。
 * 开通：第一步（用户中心建租户和首个管理员）同步做完，临时密码只显示这一次；设份额、装默认技能后台接着做。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Checkbox, Form, Input, InputNumber, Modal, Select, Space, Switch, Table, Tag, Typography } from "antd";
import Link from "next/link";
import { useState } from "react";

import { ErrorBox, OneTimeSecret, PageHead } from "@/components/common";
import { UC, api, sys } from "@/lib/api";
import { GB, KIND_LABEL, TENANT_STATUS, dateTime, limit } from "@/lib/format";
import { useMe, useSystems, withCapability } from "@/lib/session";
import type { AppInfo, OpenResult, Quotas, SystemInfo, Tenant } from "@/lib/types";

export default function TenantsPage() {
  const { data: me } = useMe();
  const q = useQuery({ queryKey: ["tenants"], queryFn: () => api.get<Tenant[]>("/tenants"), refetchInterval: 10_000 });
  const [open, setOpen] = useState(false);

  return (
    <>
      <PageHead
        title="租户"
        sub="所有租户一律平等。开通、停用、恢复、关闭都按顺序通知各系统，失败的步骤后台自动重试"
        extra={
          me?.role === "ADMIN" && (
            <Button type="primary" onClick={() => setOpen(true)}>
              开通租户
            </Button>
          )
        }
      />
      <ErrorBox error={q.error} />
      <Table<Tenant>
        rowKey="id"
        loading={q.isLoading}
        dataSource={q.data ?? []}
        pagination={{ pageSize: 20 }}
        columns={[
          {
            title: "企业代码",
            dataIndex: "code",
            render: (code: string, t) => <Link href={`/tenants/${t.id}`}>{code}</Link>,
          },
          { title: "名称", dataIndex: "name" },
          {
            title: "状态",
            render: (_, t) => {
              const s = TENANT_STATUS[t.status] ?? { text: t.status, color: "default" };
              return (
                <Space size={4}>
                  <Tag color={s.color}>{s.text}</Tag>
                  {t.status === "active" && !t.usable && <Tag color="red">已到期</Tag>}
                </Space>
              );
            },
          },
          { title: "人数 / 名额", render: (_, t) => `${t.users ?? 0} / ${limit(t.max_users)}` },
          { title: "到期", dataIndex: "expires_at", render: (v: string | null) => (v ? dateTime(v) : "不到期") },
          {
            title: "进行中的操作",
            render: (_, t) =>
              t.operation ? (
                <Space size={4}>
                  <Tag color="processing">{KIND_LABEL[t.operation.kind]}</Tag>
                  {t.operation.stuck && <Tag color="red">有步骤失败，重试中</Tag>}
                </Space>
              ) : (
                <Typography.Text type="secondary">—</Typography.Text>
              ),
          },
          { title: "开通时间", dataIndex: "created_at", render: dateTime },
        ]}
      />
      {open && <OpenTenant onClose={() => setOpen(false)} />}
    </>
  );
}

interface OpenForm {
  code: string;
  name: string;
  admin_account: string;
  admin_name: string;
  admin_email: string;
  max_users?: number | null;
  expires_on?: string;
  apps: string[];
  install_default_skills: boolean;
}

/** 份额字段：按这个系统的默认份额里有哪几项来决定（Atlas 三项，知识库只有存储） */
const QUOTA_FIELDS: Record<string, { label: string; unit?: "GB" }> = {
  running_runs: { label: "同时运行的 run" },
  sandboxes: { label: "沙箱 / 会话 Pod" },
  storage_bytes: { label: "存储", unit: "GB" },
};

function OpenTenant({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [form] = Form.useForm<OpenForm>();
  const { data: systems } = useSystems();
  const capacity = withCapability(systems, "capacity");
  const apps = useQuery({ queryKey: ["apps"], queryFn: () => sys(UC).get<AppInfo[]>("/apps") });
  const defaults = useQuery({
    queryKey: ["quota-defaults", capacity.map((s) => s.code)],
    enabled: capacity.length > 0,
    queryFn: async () =>
      Object.fromEntries(
        await Promise.all(capacity.map(async (s) => [s.code, (await sys(s.code).get<Quotas>("/quotas")).default] as const)),
      ),
  });
  /** 系统编码 → 单独定的份额（没在里面 = 用默认份额） */
  const [custom, setCustom] = useState<Record<string, Record<string, number | null>>>({});
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<OpenResult | null>(null);

  async function submit(v: OpenForm) {
    setBusy(true);
    setError(null);
    try {
      const quotas = Object.fromEntries(
        Object.entries(custom).map(([code, q]) => [
          code,
          Object.fromEntries(
            Object.entries(q).map(([k, n]) => [k, n === null || n === undefined ? null : QUOTA_FIELDS[k]?.unit ? Math.round(n * GB) : n]),
          ),
        ]),
      );
      const out = await api.post<OpenResult>("/tenants", {
        code: v.code,
        name: v.name,
        admin: { account: v.admin_account, name: v.admin_name, email: v.admin_email },
        max_users: v.max_users || null,
        expires_at: v.expires_on ? new Date(`${v.expires_on}T23:59:59`).toISOString() : null,
        apps: v.apps ?? [],
        quotas,
        install_default_skills: v.install_default_skills,
      });
      setResult(out);
      await qc.invalidateQueries({ queryKey: ["tenants"] });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  if (result) {
    return (
      <Modal open title="租户已开通" onCancel={onClose} footer={<Button onClick={onClose}>我已交给租户管理员</Button>}>
        <Space direction="vertical" style={{ width: "100%" }}>
          {result.tenant ? (
            <>
              <Typography.Text>
                企业代码 <b>{result.tenant.code}</b>，首个管理员账号 <b>{result.admin?.account}</b>
              </Typography.Text>
              {result.temp_password && <OneTimeSecret label="首个管理员的临时密码" value={result.temp_password} />}
              <Typography.Text type="secondary">
                设份额、装默认技能在后台接着做，进度在租户详情里看。
              </Typography.Text>
            </>
          ) : (
            <Typography.Text type="warning">{result.message}</Typography.Text>
          )}
        </Space>
      </Modal>
    );
  }

  return (
    <Modal open title="开通租户" width={680} onCancel={onClose} onOk={() => form.submit()} okText="开通" confirmLoading={busy}>
      <ErrorBox error={error} />
      <Form<OpenForm>
        form={form}
        layout="vertical"
        requiredMark={false}
        initialValues={{ apps: [], install_default_skills: true }}
        onFinish={submit}
      >
        <Space.Compact block style={{ gap: 12 }}>
          <Form.Item
            name="code"
            label="企业代码"
            style={{ flex: 1 }}
            extra="登录时填写；小写字母开头，2–32 位，建好后不能改"
            rules={[{ required: true, pattern: /^[a-z][a-z0-9-]{1,31}$/, message: "小写字母开头，小写字母、数字与 -" }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="name" label="名称" style={{ flex: 1 }} rules={[{ required: true }]}>
            <Input />
          </Form.Item>
        </Space.Compact>
        <Typography.Text strong>首个租户管理员</Typography.Text>
        <Space.Compact block style={{ gap: 12, marginTop: 8 }}>
          <Form.Item name="admin_account" label="账号" style={{ flex: 1 }} rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="admin_name" label="姓名" style={{ flex: 1 }} rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="admin_email" label="邮箱" style={{ flex: 1.4 }} rules={[{ required: true, type: "email" }]}>
            <Input />
          </Form.Item>
        </Space.Compact>
        <Space.Compact block style={{ gap: 12 }}>
          <Form.Item name="max_users" label="人员名额" style={{ flex: 1 }} extra="不填 = 不限">
            <InputNumber min={1} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="expires_on" label="到期日" style={{ flex: 1 }} extra="不填 = 不到期；到期后自动停用">
            <Input type="date" />
          </Form.Item>
        </Space.Compact>
        <Form.Item name="apps" label="开通的应用" extra="开通后由租户管理员自己定哪些人能用">
          <Select
            mode="multiple"
            loading={apps.isLoading}
            options={(apps.data ?? [])
              .filter((a) => !a.is_builtin && a.code && a.status === "active")
              .map((a) => ({ value: a.code as string, label: `${a.icon ?? ""} ${a.name}` }))}
          />
        </Form.Item>
        <Typography.Text strong>份额</Typography.Text>
        {capacity.map((s) => (
          <QuotaEditor
            key={s.code}
            system={s}
            fields={Object.keys(defaults.data?.[s.code] ?? {})}
            value={custom[s.code]}
            onChange={(q) =>
              setCustom((c) => {
                const next = { ...c };
                if (q) next[s.code] = q;
                else delete next[s.code];
                return next;
              })
            }
          />
        ))}
        <Form.Item name="install_default_skills" valuePropName="checked" style={{ marginTop: 12 }}>
          <Checkbox>从技能市场装「创建租户时默认安装」的技能</Checkbox>
        </Form.Item>
      </Form>
    </Modal>
  );
}

function QuotaEditor({
  system,
  fields,
  value,
  onChange,
}: {
  system: SystemInfo;
  fields: string[];
  value: Record<string, number | null> | undefined;
  onChange: (q: Record<string, number | null> | undefined) => void;
}) {
  return (
    <div style={{ border: "1px solid #eee", borderRadius: 8, padding: "8px 12px", marginTop: 8 }}>
      <Space style={{ width: "100%", justifyContent: "space-between" }}>
        <Typography.Text>{system.name}</Typography.Text>
        <Space size={6}>
          <Typography.Text type="secondary">用默认份额</Typography.Text>
          <Switch
            size="small"
            checked={!value}
            onChange={(on) => onChange(on ? undefined : Object.fromEntries(fields.map((f) => [f, null])))}
          />
        </Space>
      </Space>
      {value && (
        <Space wrap style={{ marginTop: 8 }}>
          {fields.map((f) => (
            <Space key={f} size={4}>
              <Typography.Text type="secondary">{QUOTA_FIELDS[f]?.label ?? f}</Typography.Text>
              <InputNumber
                min={0}
                placeholder="不限"
                value={value[f] ?? null}
                addonAfter={QUOTA_FIELDS[f]?.unit}
                onChange={(n) => onChange({ ...value, [f]: n as number | null })}
                style={{ width: 150 }}
              />
            </Space>
          ))}
        </Space>
      )}
    </div>
  );
}
