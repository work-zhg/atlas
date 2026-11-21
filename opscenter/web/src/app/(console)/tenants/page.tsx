"use client";

/**
 * 租户：名单、状态以用户中心为准；运营中心只记「对租户做过什么、做到哪了」。
 * 开通：第一步（用户中心建租户和首个管理员）同步做完，临时密码只显示这一次；设份额、装默认技能后台接着做。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Checkbox, Dropdown, Form, Input, InputNumber, Modal, Segmented, Table, Tooltip } from "antd";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import {
  CopyAll,
  ErrorBox,
  Expiry,
  OBSERVER_TIP,
  OnceBox,
  PageHead,
  QUOTA_LABEL,
  Seats,
  SystemIcon,
  TenantIcon,
  TenantTags,
  daysLeft,
  quotaText,
} from "@/components/common";
import { Icon } from "@/components/Icon";
import { useTenantActions } from "@/components/tenantActions";
import { UC, api, sys } from "@/lib/api";
import { GB } from "@/lib/format";
import { useMe, useSystems, withCapability } from "@/lib/session";
import type { AppInfo, OpenResult, Quota, Quotas, SystemInfo, Tenant } from "@/lib/types";

type Filter = "all" | "active" | "expiring" | "suspended" | "closed";

const FILTERS: Record<Filter, (t: Tenant) => boolean> = {
  all: () => true,
  active: (t) => t.status === "active" && t.usable,
  // 到期：已过期，或 30 天内到期
  expiring: (t) => t.status === "active" && !!t.expires_at && (daysLeft(t.expires_at) ?? 99) <= 30,
  suspended: (t) => t.status === "suspended",
  closed: (t) => t.status === "closed",
};

export default function TenantsPage() {
  const router = useRouter();
  const { data: me } = useMe();
  const admin = me?.role === "ADMIN";
  const q = useQuery({ queryKey: ["tenants"], queryFn: () => api.get<Tenant[]>("/tenants"), refetchInterval: 10_000 });
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [kw, setKw] = useState("");
  const actions = useTenantActions(admin);

  const all = q.data ?? [];
  const rows = useMemo(() => {
    const k = kw.trim().toLowerCase();
    return all.filter(FILTERS[filter]).filter((t) => !k || t.name.toLowerCase().includes(k) || t.code.includes(k));
  }, [all, filter, kw]);
  const n = (f: Filter) => all.filter(FILTERS[f]).length;

  return (
    <>
      <PageHead
        title="租户"
        sub="每个企业一个租户：账号、数据、份额彼此隔开。运营中心只看得到元数据和数字，看不到企业的业务内容"
        extra={
          <Tooltip title={admin ? undefined : OBSERVER_TIP}>
            <Button type="primary" icon={<Icon name="plus" />} disabled={!admin} onClick={() => setOpen(true)}>
              开通租户
            </Button>
          </Tooltip>
        }
      />
      <div className="ops-toolbar">
        <Input allowClear prefix={<Icon name="search" size={14} />} placeholder="搜索名称或企业代码" style={{ width: 260 }} value={kw} onChange={(e) => setKw(e.target.value)} />
        <Segmented<Filter>
          value={filter}
          onChange={setFilter}
          options={[
            { value: "all", label: `全部 ${n("all")}` },
            { value: "active", label: `正常 ${n("active")}` },
            { value: "expiring", label: `到期 ${n("expiring")}` },
            { value: "suspended", label: `已停用 ${n("suspended")}` },
            { value: "closed", label: `已关闭 ${n("closed")}` },
          ]}
        />
      </div>
      <ErrorBox error={q.error} />
      <Table<Tenant>
        rowKey="id"
        loading={q.isLoading}
        dataSource={rows}
        pagination={rows.length > 20 ? { pageSize: 20 } : false}
        onRow={(t) => ({ onClick: () => router.push(`/tenants/${t.id}`), className: "ops-row-click" })}
        locale={{ emptyText: "没有符合条件的租户" }}
        columns={[
          {
            title: "租户",
            render: (_, t) => (
              <div className="ops-row" style={{ gap: 10 }}>
                <TenantIcon tenant={t} />
                <div>
                  <b className="ops-nw">{t.name}</b>
                  <div className="ops-sub ops-mono">{t.code}</div>
                </div>
              </div>
            ),
          },
          {
            title: "状态",
            render: (_, t) => (
              <div className="ops-row" style={{ gap: 4, flexWrap: "wrap" }}>
                <TenantTags tenant={t} />
              </div>
            ),
          },
          { title: "人数 / 名额", render: (_, t) => <Seats tenant={t} /> },
          { title: "到期", render: (_, t) => <Expiry tenant={t} /> },
          { title: "开通时间", render: (_, t) => <span className="ops-muted ops-nw">{t.created_at.slice(0, 10)}</span> },
          {
            title: "",
            width: 48,
            align: "right",
            render: (_, t) =>
              t.status !== "closed" && (
                <span onClick={(e) => e.stopPropagation()}>
                  <Dropdown trigger={["click"]} placement="bottomRight" menu={actions.menu(t, { withView: () => router.push(`/tenants/${t.id}`) })}>
                    <Button type="text" size="small" aria-label="更多" icon={<Icon name="more" />} />
                  </Dropdown>
                </span>
              ),
          },
        ]}
      />
      {open && <OpenTenant onClose={() => setOpen(false)} onOpened={(id) => router.push(`/tenants/${id}`)} />}
      {actions.element}
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

function OpenTenant({ onClose, onOpened }: { onClose: () => void; onOpened: (id: string) => void }) {
  const qc = useQueryClient();
  const [form] = Form.useForm<OpenForm>();
  const { data: systems } = useSystems();
  const capacity = withCapability(systems, "capacity");
  const apps = useQuery({ queryKey: ["apps"], queryFn: () => sys(UC).get<AppInfo[]>("/apps") });
  const defaults = useQuery({
    queryKey: ["quota-defaults", capacity.map((s) => s.code)],
    enabled: capacity.length > 0,
    queryFn: async () =>
      Object.fromEntries(await Promise.all(capacity.map(async (s) => [s.code, (await sys(s.code).get<Quotas>("/quotas")).default] as const))),
  });
  /** 系统编码 → 单独定的份额（没在里面 = 用默认份额） */
  const [custom, setCustom] = useState<Record<string, Record<string, number | null>>>({});
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<OpenResult | null>(null);
  const optional = (apps.data ?? []).filter((a) => !a.is_builtin && a.code && a.status === "active");
  const builtin = (apps.data ?? []).filter((a) => a.is_builtin);

  async function submit(v: OpenForm) {
    setBusy(true);
    setError(null);
    try {
      const quotas = Object.fromEntries(
        Object.entries(custom).map(([code, q]) => [
          code,
          Object.fromEntries(Object.entries(q).map(([k, n]) => [k, n === null || n === undefined ? null : k === "storage_bytes" ? Math.round(n * GB) : n])),
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
    const uc = (apps.data ?? []).find((a) => a.code === "usercenter")?.home_url ?? "/uc";
    const rows: [string, string][] = result.tenant
      ? [
          ["登录地址", `${window.location.origin}${uc}/login`],
          ["企业代码", result.tenant.code],
          ["管理员账号", result.admin?.account ?? ""],
          ...(result.temp_password ? ([["临时密码", result.temp_password]] as [string, string][]) : []),
        ]
      : [];
    const done = () => (result.tenant ? onOpened(result.tenant.id) : onClose());
    return (
      <Modal
        open
        title={result.tenant ? "租户已开通" : "开通没完成"}
        maskClosable={false}
        onCancel={done}
        footer={
          result.tenant ? (
            <>
              <CopyAll rows={rows} />
              <Button type="primary" onClick={done}>
                我已交给企业管理员
              </Button>
            </>
          ) : (
            <Button onClick={onClose}>关闭</Button>
          )
        }
      >
        {result.tenant ? (
          <>
            <div className="ops-row" style={{ alignItems: "flex-start", gap: 8, marginBottom: 12, color: "var(--primary-text)" }}>
              <Icon name="checkCircle" style={{ marginTop: 3 }} />
              <span>用户中心已建好租户和首个管理员；设份额、装默认技能在后台接着做，进度在租户的「操作记录」里</span>
            </div>
            <OnceBox rows={rows} warn="临时密码只显示这一次，关掉就再也看不到。交给企业管理员，他第一次登录要改成自己的密码" />
          </>
        ) : (
          <ErrorBox error={new Error(result.message ?? "用户中心没有建好租户，后台在重试")} />
        )}
      </Modal>
    );
  }

  return (
    <Modal
      open
      title="开通租户"
      width={720}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText="开通"
      confirmLoading={busy}
      maskClosable={false}
      footer={(origin) => (
        <div className="ops-row">
          <span className="ops-sub ops-grow" style={{ textAlign: "left" }}>
            开通依次在各系统里做；某一步失败会自动重试，不用重新开
          </span>
          {origin}
        </div>
      )}
    >
      <ErrorBox error={error} />
      <Form<OpenForm> form={form} layout="vertical" requiredMark={false} initialValues={{ apps: [], install_default_skills: true }} onFinish={submit}>
        <div className="ops-section">企业</div>
        <div className="ops-cols-2">
          <Form.Item
            name="code"
            label="企业代码"
            extra="登录时输入；小写字母开头，2–32 位，建好不能改"
            rules={[{ required: true, pattern: /^[a-z][a-z0-9-]{1,31}$/, message: "小写字母开头，小写字母、数字与 -" }]}
          >
            <Input placeholder="如 beichen" />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请输入名称" }]}>
            <Input />
          </Form.Item>
        </div>
        <div className="ops-section">
          首个企业管理员<span className="ops-sub">开通后由他在用户中心建人、授权</span>
        </div>
        <div className="ops-cols-3">
          <Form.Item name="admin_account" label="账号" rules={[{ required: true, message: "请输入账号" }]}>
            <Input />
          </Form.Item>
          <Form.Item name="admin_name" label="姓名" rules={[{ required: true, message: "请输入姓名" }]}>
            <Input />
          </Form.Item>
          <Form.Item name="admin_email" label="邮箱" rules={[{ required: true, type: "email", message: "请输入邮箱" }]}>
            <Input />
          </Form.Item>
        </div>
        <div className="ops-section">名额与到期</div>
        <div className="ops-cols-2">
          <Form.Item name="max_users" label="人员名额" extra="不填 = 不限">
            <InputNumber min={1} style={{ width: "100%" }} placeholder="不限" />
          </Form.Item>
          <Form.Item name="expires_on" label="到期日" extra="不填 = 不到期；到期后自动停用">
            <Input type="date" />
          </Form.Item>
        </div>
        <div className="ops-section">
          开通的应用
          <span className="ops-sub">
            {builtin.length ? `${builtin.map((a) => a.name).join("、")}是产品自带的，所有企业都能用；` : ""}谁能用由企业管理员定
          </span>
        </div>
        {optional.length ? (
          <Form.Item name="apps">
            <Checkbox.Group style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 8 }}>
              {optional.map((a) => (
                <Checkbox key={a.code} value={a.code}>
                  {a.icon ?? ""} {a.name}
                </Checkbox>
              ))}
            </Checkbox.Group>
          </Form.Item>
        ) : (
          <div className="ops-sub" style={{ marginBottom: 16 }}>
            还没有接入别的应用
          </div>
        )}
        <div className="ops-section">份额</div>
        {capacity.map((s) => (
          <QuotaEditor
            key={s.code}
            system={s}
            defaults={defaults.data?.[s.code]}
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
        <Form.Item name="install_default_skills" valuePropName="checked" style={{ marginTop: 12, marginBottom: 0 }}>
          <Checkbox>装技能市场里「开通时默认安装」的技能</Checkbox>
        </Form.Item>
      </Form>
    </Modal>
  );
}

function QuotaEditor({
  system,
  defaults,
  value,
  onChange,
}: {
  system: SystemInfo;
  defaults: Quota | undefined;
  value: Record<string, number | null> | undefined;
  onChange: (q: Record<string, number | null> | undefined) => void;
}) {
  const fields = Object.keys(defaults ?? {});
  return (
    <div className="ops-quota-row">
      <SystemIcon code={system.code} size={24} />
      <b style={{ width: 72 }}>{system.name}</b>
      <Segmented
        size="small"
        value={value ? "own" : "default"}
        onChange={(v) => onChange(v === "default" ? undefined : Object.fromEntries(fields.map((f) => [f, null])))}
        options={[
          { value: "default", label: "用默认" },
          { value: "own", label: "单独定" },
        ]}
      />
      {value ? (
        fields.map((f) => (
          <InputNumber
            key={f}
            size="small"
            min={0}
            placeholder="不限"
            value={value[f] ?? null}
            addonBefore={QUOTA_LABEL[f] ?? f}
            addonAfter={f === "storage_bytes" ? "GB" : undefined}
            onChange={(n) => onChange({ ...value, [f]: n as number | null })}
            style={{ width: f === "storage_bytes" ? 190 : 180 }}
          />
        ))
      ) : (
        <span className="ops-sub">{fields.map((f) => `${QUOTA_LABEL[f] ?? f} ${quotaText(f, defaults?.[f as keyof Quota])}`).join(" · ")}</span>
      )}
    </div>
  );
}
