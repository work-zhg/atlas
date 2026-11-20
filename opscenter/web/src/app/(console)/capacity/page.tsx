"use client";

/**
 * 容量：总容量 → 租户份额 → 每人上限（每人上限由租户管理员在份额内自己定，不在这里）。
 * 各系统各管自己的共用设施（Atlas：并发 run、沙箱、工作空间存储；知识库：知识库文件），只有数字。
 * 份额可以多分：总量占满时所有租户按先来后到排队。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, InputNumber, Modal, Space, Statistic, Table, Typography } from "antd";
import Link from "next/link";
import { useState } from "react";

import { ErrorBox, PageHead, errorText } from "@/components/common";
import { api, sys } from "@/lib/api";
import { GB, bytes, dateTime, limit } from "@/lib/format";
import { useMe, useSystems, withCapability } from "@/lib/session";
import type { CapacityTotal, Quota, Quotas, SystemInfo, Tenant } from "@/lib/types";

const LABEL: Record<string, string> = { running_runs: "同时运行的 run", sandboxes: "沙箱 / 会话 Pod", storage_bytes: "存储" };
const fmt = (k: string, v: number | null | undefined) => (k === "storage_bytes" ? bytes(v) : limit(v));

export default function CapacityPage() {
  const { data: systems } = useSystems();
  const tenants = useQuery({ queryKey: ["tenants"], queryFn: () => api.get<Tenant[]>("/tenants") });
  return (
    <>
      <PageHead title="容量" sub="总容量、默认份额和各租户单独定的份额。租户里每个人的上限由租户管理员在份额内自己定" />
      <Space direction="vertical" style={{ width: "100%" }} size={16}>
        {withCapability(systems, "capacity").map((s) => (
          <SystemCapacity key={s.code} system={s} tenants={tenants.data ?? []} />
        ))}
      </Space>
    </>
  );
}

function SystemCapacity({ system, tenants }: { system: SystemInfo; tenants: Tenant[] }) {
  const { data: me } = useMe();
  const qc = useQueryClient();
  const { message } = App.useApp();
  const ops = sys(system.code);
  const total = useQuery({ queryKey: ["capacity", system.code], queryFn: () => ops.get<CapacityTotal>("/capacity") });
  const quotas = useQuery({ queryKey: ["quotas", system.code], queryFn: () => ops.get<Quotas>("/quotas") });
  const [editing, setEditing] = useState<Record<string, number | null> | null>(null);
  const fields = Object.keys(quotas.data?.default ?? {});
  const tenantName = (id: string) => tenants.find((t) => t.id === id);

  async function saveDefault() {
    if (!editing) return;
    try {
      await ops.put(
        "/quotas/default",
        Object.fromEntries(Object.entries(editing).map(([k, v]) => [k, v === null ? null : k === "storage_bytes" ? Math.round(v * GB) : v])),
      );
      setEditing(null);
      await qc.invalidateQueries({ queryKey: ["quotas", system.code] });
      message.success("默认份额已保存（没单独定份额的租户都跟着变）");
    } catch (e) {
      message.error(errorText(e));
    }
  }

  return (
    <Card title={system.name}>
      <ErrorBox error={total.error ?? quotas.error} />
      <Space size={48} style={{ marginBottom: 16 }}>
        {total.data?.runs_per_instance !== undefined && (
          <Statistic title="每个实例的并发 run 槽位" value={total.data.runs_per_instance} />
        )}
        {Object.entries(total.data?.usage ?? {}).map(([k, v]) => (
          <Statistic key={k} title={`当前用量 · ${LABEL[k] ?? k}`} value={fmt(k, v)} />
        ))}
      </Space>
      <Space style={{ marginBottom: 8 }}>
        <Typography.Text strong>默认份额</Typography.Text>
        <Typography.Text>
          {fields.map((k) => `${LABEL[k] ?? k} ${fmt(k, quotas.data?.default[k as keyof Quota])}`).join(" · ")}
        </Typography.Text>
        {me?.role === "ADMIN" && (
          <Button
            size="small"
            onClick={() =>
              setEditing(
                Object.fromEntries(
                  fields.map((k) => {
                    const v = quotas.data?.default[k as keyof Quota] ?? null;
                    return [k, k === "storage_bytes" && v !== null ? v / GB : v];
                  }),
                ),
              )
            }
          >
            改默认份额
          </Button>
        )}
      </Space>
      <Table
        rowKey="tenant_uuid"
        size="small"
        pagination={false}
        loading={quotas.isLoading}
        dataSource={quotas.data?.tenants ?? []}
        locale={{ emptyText: "没有租户单独定份额，都用默认份额" }}
        columns={[
          {
            title: "单独定了份额的租户",
            dataIndex: "tenant_uuid",
            render: (id: string) => {
              const t = tenantName(id);
              return <Link href={`/tenants/${id}`}>{t ? `${t.name}（${t.code}）` : id}</Link>;
            },
          },
          ...fields.map((k) => ({
            title: LABEL[k] ?? k,
            dataIndex: k,
            render: (v: number | null) => fmt(k, v),
          })),
          { title: "改的人", dataIndex: "updated_by" },
          { title: "改的时间", dataIndex: "updated_at", render: dateTime },
        ]}
      />
      {editing && (
        <Modal open title={`${system.name} · 默认份额`} onCancel={() => setEditing(null)} onOk={saveDefault}>
          <Space direction="vertical">
            {Object.entries(editing).map(([k, v]) => (
              <Space key={k}>
                <span style={{ width: 120, display: "inline-block" }}>{LABEL[k] ?? k}</span>
                <InputNumber
                  min={0}
                  placeholder="不限"
                  value={v}
                  addonAfter={k === "storage_bytes" ? "GB" : undefined}
                  onChange={(n) => setEditing({ ...editing, [k]: (n as number | null) ?? null })}
                />
              </Space>
            ))}
            <Typography.Text type="secondary">留空 = 不限。改默认份额是改系统参数，有修订历史</Typography.Text>
          </Space>
        </Modal>
      )}
    </Card>
  );
}
