"use client";

/**
 * 份额与容量：总量 → 企业份额 → 每人上限（每人上限由企业管理员在份额内自己定，不在这里）。
 * 各系统各管自己的共用设施（Atlas：同时运行的对话、沙箱、工作空间存储；知识库：知识库文件），只有数字。
 * 份额可以多分：总量占满时所有企业按先来后到排队。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Space, Table, Tooltip } from "antd";
import Link from "next/link";
import { useState } from "react";

import { ErrorBox, OBSERVER_TIP, PageHead, QUOTA_LABEL, SystemIcon, errorText, quotaText } from "@/components/common";
import { Icon } from "@/components/Icon";
import { QuotaModal } from "@/components/quota";
import { api, sys } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { useAccess, useSystems, withCapability } from "@/lib/session";
import type { CapacityTotal, Quota, Quotas, SystemInfo, Tenant, TenantUsage } from "@/lib/types";

export default function CapacityPage() {
  const { data: systems } = useSystems();
  const tenants = useQuery({ queryKey: ["tenants"], queryFn: () => api.get<Tenant[]>("/tenants") });
  return (
    <>
      <PageHead title="份额与容量" sub="份额管的是每个企业最多能占多少共用资源（同时运行的对话、沙箱、存储）。没单独定的企业用默认份额；企业里每个人的上限由企业管理员在份额内自己定" />
      <Space direction="vertical" style={{ width: "100%" }} size={16}>
        {withCapability(systems, "capacity").map((s) => (
          <SystemCapacity key={s.code} system={s} tenants={tenants.data ?? []} />
        ))}
      </Space>
    </>
  );
}

type Own = Quota & { tenant_uuid: string; updated_by: string; updated_at: string };

function SystemCapacity({ system, tenants }: { system: SystemInfo; tenants: Tenant[] }) {
  // 份额存在各系统里：改这个系统的份额，要这个系统的模块读写
  const acc = useAccess();
  const admin = acc.write(system.module);
  const qc = useQueryClient();
  const { message } = App.useApp();
  const ops = sys(system.code);
  const total = useQuery({ queryKey: ["capacity", system.code], queryFn: () => ops.get<CapacityTotal>("/capacity") });
  const quotas = useQuery({ queryKey: ["quotas", system.code], queryFn: () => ops.get<Quotas>("/quotas") });
  const [editing, setEditing] = useState<"default" | Own | null>(null);
  const fields = Object.keys(quotas.data?.default ?? {});
  const tenantOf = (id: string) => tenants.find((t) => t.id === id);

  async function save(path: string, body: Record<string, unknown>, ok: string) {
    try {
      await ops.put(path, body);
      await qc.invalidateQueries({ queryKey: ["quotas", system.code] });
      await qc.invalidateQueries({ queryKey: ["usage", system.code] });
      message.success(ok);
    } catch (e) {
      message.error(errorText(e));
      throw e;
    }
  }

  const stats: [string, string][] = [
    ...(total.data?.runs_per_instance !== undefined ? ([["每个实例的并发槽位", String(total.data.runs_per_instance)]] as [string, string][]) : []),
    ...Object.entries(total.data?.usage ?? {}).map(([k, v]) => [`当前用量 · ${QUOTA_LABEL[k] ?? k}`, quotaText(k, v)] as [string, string]),
  ];
  const own = (quotas.data?.tenants ?? []) as Own[];

  return (
    <Card
      title={
        <div className="ops-row">
          <SystemIcon code={system.code} size={26} />
          {system.name}
        </div>
      }
    >
      <ErrorBox error={total.error ?? quotas.error} />
      <div className="ops-stats" style={{ gridTemplateColumns: `repeat(${Math.max(stats.length, 1)}, minmax(0, 1fr))`, marginTop: 0 }}>
        {stats.map(([k, v]) => (
          <div key={k} className="ops-stat">
            <div className="k">{k}</div>
            <div className="v">{v}</div>
          </div>
        ))}
      </div>
      <div className="ops-def-row">
        <b>默认份额</b>
        {fields.map((k) => (
          <span key={k} className="ops-chip">
            {QUOTA_LABEL[k] ?? k} <b>{quotaText(k, quotas.data?.default[k as keyof Quota])}</b>
          </span>
        ))}
        <span className="ops-grow" />
        <Tooltip title={admin ? undefined : OBSERVER_TIP}>
          <Button icon={<Icon name="pencil" size={14} />} disabled={!admin || !quotas.data} onClick={() => setEditing("default")}>
            改默认份额
          </Button>
        </Tooltip>
      </div>
      <div className="ops-subhead">单独定了份额的企业 {own.length}</div>
      <Table<Own>
        rowKey="tenant_uuid"
        pagination={false}
        loading={quotas.isLoading}
        dataSource={own}
        locale={{ emptyText: "没有企业单独定份额，都用默认份额" }}
        columns={[
          {
            title: "企业",
            render: (_, r) => {
              const t = tenantOf(r.tenant_uuid);
              return (
                <Link href={`/tenants/${r.tenant_uuid}`}>
                  {t ? t.name : r.tenant_uuid} {t && <span className="ops-sub ops-mono">{t.code}</span>}
                </Link>
              );
            },
          },
          ...fields.map((k) => ({
            title: QUOTA_LABEL[k] ?? k,
            render: (_: unknown, r: Own) => <UsedOf system={system.code} tenantId={r.tenant_uuid} field={k} cap={r[k as keyof Quota]} />,
          })),
          { title: "改的人 · 时间", render: (_, r) => <span className="ops-muted">{r.updated_by} · {dateTime(r.updated_at)}</span> },
          {
            title: "",
            align: "right",
            render: (_, r) => (
              <Space size={0}>
                <Button type="link" size="small" disabled={!admin} onClick={() => setEditing(r)}>
                  修改
                </Button>
                <Button
                  type="link"
                  size="small"
                  disabled={!admin}
                  onClick={() => void save(`/quotas/${r.tenant_uuid}`, { use_default: true }, "已改回默认份额").catch(() => undefined)}
                >
                  改回默认
                </Button>
              </Space>
            ),
          },
        ]}
      />
      {editing === "default" && quotas.data && (
        <QuotaModal
          title={`${system.name} · 默认份额`}
          quota={quotas.data.default}
          hint="留空 = 不限。改默认份额是改系统参数，有修订历史；单独定了份额的企业不受影响"
          onSave={(body) => save("/quotas/default", body, "默认份额已保存（没单独定份额的企业都跟着变）")}
          onClose={() => setEditing(null)}
        />
      )}
      {editing && editing !== "default" && (
        <QuotaModal
          title={`${system.name} · ${tenantOf(editing.tenant_uuid)?.name ?? "企业"}的份额`}
          quota={Object.fromEntries(fields.map((k) => [k, editing[k as keyof Quota] ?? null]))}
          hint="留空 = 不限；只对这个企业生效"
          onSave={(body) => save(`/quotas/${editing.tenant_uuid}`, body, "份额已保存")}
          onClose={() => setEditing(null)}
        />
      )}
    </Card>
  );
}

/** 用量 / 份额：用量逐个企业查（单独定份额的企业不多） */
function UsedOf({ system, tenantId, field, cap }: { system: string; tenantId: string; field: string; cap: number | null | undefined }) {
  const q = useQuery({ queryKey: ["usage", system, tenantId], queryFn: () => sys(system).get<TenantUsage>(`/usage/${tenantId}`) });
  const used = q.data?.usage[field as keyof TenantUsage["usage"]];
  return (
    <span className="ops-nw">
      {used === undefined ? "…" : quotaText(field, used)} <span className="ops-muted">/ {quotaText(field, cap)}</span>
    </span>
  );
}
