"use client";

/**
 * 一个租户：概况（基本信息 + 用量）、应用、份额与用量（只有数字）、操作记录（做过的操作和各系统的进度）。
 * 停用先挡用户中心的入口再停用各系统；关闭只能对已停用的做，要输入企业代码确认，不可恢复。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Dropdown, Space, Switch, Table, Tabs, Tag, Tooltip, Typography } from "antd";
import { useParams } from "next/navigation";
import { useState } from "react";

import {
  DetailHead,
  ErrorBox,
  Expiry,
  OBSERVER_TIP,
  QUOTA_LABEL,
  Seats,
  SystemIcon,
  TenantIcon,
  TenantTags,
  UsageBar,
  AppIcon,
  errorText,
} from "@/components/common";
import { Icon } from "@/components/Icon";
import { QuotaModal } from "@/components/quota";
import { useTenantActions } from "@/components/tenantActions";
import { UC, api, sys } from "@/lib/api";
import { ACTION_LABEL, KIND_LABEL, dateTime } from "@/lib/format";
import { TENANT_MODULE, systemName, useAccess, useSystems, withCapability } from "@/lib/session";
import type { Operation, SystemInfo, Tenant, TenantApp, TenantUsage } from "@/lib/types";

interface Detail {
  tenant: Tenant;
  operations: Operation[];
}

export default function TenantPage() {
  const { id } = useParams<{ id: string }>();
  const acc = useAccess();
  const admin = acc.write(TENANT_MODULE);
  const q = useQuery({
    queryKey: ["tenant", id],
    queryFn: () => api.get<Detail>(`/tenants/${id}`),
    refetchInterval: (query) => (query.state.data?.operations.some((o) => o.status === "RUNNING") ? 3000 : 15000),
  });
  const { data: systems } = useSystems();
  const [tab, setTab] = useState("overview");
  const actions = useTenantActions(admin, acc.owner(TENANT_MODULE));
  const ops = q.data?.operations ?? [];
  const running = ops.find((o) => o.status === "RUNNING");
  // 详情接口不带进行中的操作摘要：从操作记录里补上，标签、菜单的「一次只做一件」都按它判断
  const t: Tenant | undefined = q.data && { ...q.data.tenant, operation: running ? { id: running.id, kind: running.kind, stuck: running.stuck } : null };

  if (q.error) return <ErrorBox error={q.error} />;
  if (!t) return null;
  const adminAccount = ops
    .filter((o) => o.kind === "OPEN")
    .flatMap((o) => o.steps)
    .map((s) => s.result?.admin_account)
    .find((a): a is string => typeof a === "string");
  const lastSuspend = ops.find((o) => o.kind === "SUSPEND");

  return (
    <>
      <DetailHead
        back="/tenants"
        icon={<TenantIcon tenant={t} size={40} />}
        title={t.name}
        tags={<TenantTags tenant={t} />}
        sub={
          <>
            企业代码 <span className="ops-mono">{t.code}</span> · 开通于 {t.created_at.slice(0, 10)}
          </>
        }
        extra={
          t.status !== "closed" && (
            <>
              <Tooltip title={admin ? undefined : OBSERVER_TIP}>
                <Button icon={<Icon name="pencil" size={14} />} disabled={!admin} onClick={() => actions.edit(t)}>
                  编辑
                </Button>
              </Tooltip>
              {t.status === "suspended" && (
                <Tooltip title={admin ? undefined : OBSERVER_TIP}>
                  <Button icon={<Icon name="play" size={14} />} disabled={!admin || !!running} onClick={() => void actions.resume(t)}>
                    恢复
                  </Button>
                </Tooltip>
              )}
              <Dropdown trigger={["click"]} placement="bottomRight" menu={actions.menu(t)}>
                <Button aria-label="更多" icon={<Icon name="more" />} />
              </Dropdown>
            </>
          )
        }
      />
      {running && (
        <Alert
          style={{ margin: "12px 0 4px" }}
          type={running.stuck ? "error" : "info"}
          showIcon
          message={
            <>
              {KIND_LABEL[running.kind]}还没做完：{running.steps.filter((s) => s.status === "DONE").length} / {running.steps.length} 步已完成
              {running.stuck ? "，有一步失败了，在自动重试" : ""}。 <a onClick={() => setTab("ops")}>看进度</a>
            </>
          }
        />
      )}
      {t.status === "suspended" && !running && (
        <Alert
          style={{ margin: "12px 0 4px" }}
          type="warning"
          showIcon
          message={`已停用${lastSuspend?.reason ? `：${lastSuspend.reason}` : ""}。登录和各系统调用都被拒绝，数据都留着，恢复后接着用`}
        />
      )}
      {t.status === "closed" && <Alert style={{ margin: "12px 0 4px" }} type="info" showIcon message="已关闭：各系统的数据已删除，企业代码不能再用" />}
      <Tabs
        activeKey={tab}
        onChange={setTab}
        style={{ marginTop: 8 }}
        items={[
          {
            key: "overview",
            label: "概况",
            children: <Overview tenant={t} adminAccount={adminAccount} systems={withCapability(systems, "capacity")} onReissue={() => void actions.reissue(t)} admin={actions.owner} onQuota={() => setTab("quota")} />,
          },
          { key: "apps", label: "应用", children: <TenantApps tenantId={id} admin={admin && t.status !== "closed"} /> },
          { key: "quota", label: "份额与用量", children: <TenantQuotas tenantId={id} admin={t.status !== "closed"} systems={withCapability(systems, "capacity")} /> },
          { key: "ops", label: `操作记录 ${ops.length}`, children: <Operations ops={ops} systems={systems} admin={admin} /> },
        ]}
      />
      {actions.element}
    </>
  );
}

function Overview({
  tenant: t,
  adminAccount,
  systems,
  admin,
  onReissue,
  onQuota,
}: {
  tenant: Tenant;
  adminAccount: string | undefined;
  systems: SystemInfo[];
  admin: boolean;
  onReissue: () => void;
  onQuota: () => void;
}) {
  return (
    <div className="ops-detail-grid">
      <Card title="基本信息">
        <dl className="ops-kv">
          <dt>企业代码</dt>
          <dd>
            <span className="ops-mono">{t.code}</span>
            <span className="ops-sub">登录时输入，建好不能改</span>
          </dd>
          <dt>名称</dt>
          <dd>{t.name}</dd>
          <dt>状态</dt>
          <dd>
            <TenantTags tenant={t} />
          </dd>
          <dt>人数 / 名额</dt>
          <dd>
            <Seats tenant={t} />
          </dd>
          <dt>到期</dt>
          <dd>
            <Expiry tenant={t} />
          </dd>
          <dt>首个管理员</dt>
          <dd>
            <span className="ops-mono">{adminAccount ?? "—"}</span>
            {t.status === "active" && admin && <a onClick={onReissue}>补发临时密码</a>}
          </dd>
          <dt>开通时间</dt>
          <dd>{dateTime(t.created_at)}</dd>
          <dt>租户 id</dt>
          <dd>
            <Typography.Text copyable className="ops-mono" style={{ fontSize: 12 }}>
              {t.id}
            </Typography.Text>
          </dd>
        </dl>
      </Card>
      <Card
        title="用量"
        extra={
          <a onClick={onQuota}>
            份额与用量 <Icon name="right" size={12} />
          </a>
        }
      >
        {t.status === "closed" ? (
          <span className="ops-muted">已关闭，数据已删除</span>
        ) : (
          <Space direction="vertical" size={14} style={{ width: "100%" }}>
            {systems.map((s) => (
              <UsageRows key={s.code} system={s} tenantId={t.id} prefix />
            ))}
          </Space>
        )}
      </Card>
    </div>
  );
}

/** 一个系统的用量 / 份额（Atlas 三项，知识库只有存储） */
function UsageRows({ system, tenantId, prefix }: { system: SystemInfo; tenantId: string; prefix?: boolean }) {
  const q = useQuery({ queryKey: ["usage", system.code, tenantId], queryFn: () => sys(system.code).get<TenantUsage>(`/usage/${tenantId}`) });
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data) return null;
  const u = q.data;
  return (
    <Space direction="vertical" size={14} style={{ width: "100%" }}>
      {Object.keys(u.quota).map((f) => (
        <UsageBar
          key={f}
          label={prefix ? `${system.name} · ${QUOTA_LABEL[f] ?? f}` : (QUOTA_LABEL[f] ?? f)}
          field={f}
          used={u.usage[f as keyof TenantUsage["usage"]] ?? 0}
          cap={u.quota[f as keyof TenantUsage["quota"]]}
        />
      ))}
    </Space>
  );
}

function TenantApps({ tenantId, admin }: { tenantId: string; admin: boolean }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const q = useQuery({ queryKey: ["tenant-apps", tenantId], queryFn: () => sys(UC).get<TenantApp[]>(`/tenants/${tenantId}/apps`) });
  async function toggle(appId: string, enabled: boolean) {
    try {
      await sys(UC).put(`/tenants/${tenantId}/apps/${appId}`, { enabled });
      await qc.invalidateQueries({ queryKey: ["tenant-apps", tenantId] });
      message.success(enabled ? "已开通" : "已关闭：这个企业的人马上用不了");
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Card title="开通的应用" extra={<span className="ops-sub">产品自带的应用所有企业都能用，不在这里；谁能用由企业管理员在用户中心定</span>}>
      <ErrorBox error={q.error} />
      <Table<TenantApp>
        rowKey={(r) => r.app.id}
        pagination={false}
        loading={q.isLoading}
        dataSource={q.data ?? []}
        locale={{ emptyText: "还没有接入别的应用" }}
        columns={[
          {
            title: "应用",
            render: (_, r) => (
              <div className="ops-row" style={{ gap: 10 }}>
                <AppIcon app={r.app} size={28} />
                <div>
                  <b>{r.app.name}</b>
                  <div className="ops-sub ops-mono">{r.app.code ?? "—"}</div>
                </div>
              </div>
            ),
          },
          { title: "状态", render: (_, r) => (r.app.status === "active" ? <Tag color="success">可用</Tag> : <Tag>平台已停用</Tag>) },
          { title: "开通时间", render: (_, r) => <span className="ops-muted">{r.enabled ? dateTime(r.enabled_at) : "—"}</span> },
          {
            title: "为这个企业开通",
            width: 140,
            render: (_, r) => (
              <Tooltip title={admin ? undefined : OBSERVER_TIP}>
                <Switch checked={r.enabled} disabled={!admin} onChange={(on) => void toggle(r.app.id, on)} />
              </Tooltip>
            ),
          },
        ]}
      />
    </Card>
  );
}

function TenantQuotas({ tenantId, admin, systems }: { tenantId: string; admin: boolean; systems: SystemInfo[] }) {
  const acc = useAccess();
  return (
    <Space direction="vertical" size={14} style={{ width: "100%" }}>
      {systems.map((s) => (
        // 份额存在各系统里：改哪个系统的份额，看的是那个系统的模块
        <QuotaCard key={s.code} system={s} tenantId={tenantId} admin={admin && acc.write(s.module)} />
      ))}
      <Alert type="info" showIcon message="企业里每个人的上限由企业管理员在这份额度内自己定。超了份额的新请求会被拒（已在跑的不打断）" />
    </Space>
  );
}

function QuotaCard({ system, tenantId, admin }: { system: SystemInfo; tenantId: string; admin: boolean }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const q = useQuery({ queryKey: ["usage", system.code, tenantId], queryFn: () => sys(system.code).get<TenantUsage>(`/usage/${tenantId}`) });
  const [editing, setEditing] = useState(false);
  const fields = Object.keys(q.data?.quota ?? {});

  async function save(body: Record<string, unknown>) {
    try {
      await sys(system.code).put(`/quotas/${tenantId}`, body);
      await qc.invalidateQueries({ queryKey: ["usage", system.code, tenantId] });
      await qc.invalidateQueries({ queryKey: ["quotas", system.code] });
      message.success(body.use_default ? "已改回默认份额" : "份额已保存");
    } catch (e) {
      message.error(errorText(e));
      throw e;
    }
  }

  return (
    <Card
      title={
        <div className="ops-row">
          <SystemIcon code={system.code} size={24} />
          {system.name}
          {q.data && (q.data.is_default ? <Tag>用默认份额</Tag> : <Tag color="processing">单独定的份额</Tag>)}
        </div>
      }
      extra={
        q.data && (
          <Space size={4}>
            {!q.data.is_default && (
              <Button size="small" type="text" disabled={!admin} onClick={() => void save({ use_default: true }).catch(() => undefined)}>
                改回默认
              </Button>
            )}
            <Tooltip title={admin ? undefined : OBSERVER_TIP}>
              <Button size="small" disabled={!admin} onClick={() => setEditing(true)}>
                {q.data.is_default ? "单独定份额" : "修改"}
              </Button>
            </Tooltip>
          </Space>
        )
      }
    >
      <ErrorBox error={q.error} />
      {q.data && (
        <div className="ops-ubars">
          {fields.map((f) => (
            <UsageBar
              key={f}
              label={QUOTA_LABEL[f] ?? f}
              field={f}
              used={q.data?.usage[f as keyof TenantUsage["usage"]] ?? 0}
              cap={q.data?.quota[f as keyof TenantUsage["quota"]]}
            />
          ))}
        </div>
      )}
      {editing && q.data && (
        <QuotaModal title={`${system.name} · 这个企业的份额`} quota={q.data.quota} hint="留空 = 不限；只对这个企业生效" onSave={save} onClose={() => setEditing(false)} />
      )}
    </Card>
  );
}

function Operations({ ops, systems, admin }: { ops: Operation[]; systems: SystemInfo[] | undefined; admin: boolean }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  // 默认展开最近的一次（进行中的、或刚做完的）
  const [open, setOpen] = useState<Record<string, boolean>>({});
  if (!ops.length) return <Card>还没有操作：开通、停用、恢复、关闭都会记在这里</Card>;

  async function retry(op: Operation) {
    try {
      await api.post(`/operations/${op.id}/retry`);
      message.success("马上重做失败的步骤");
      await qc.invalidateQueries({ queryKey: ["tenant", op.tenant_id] });
    } catch (e) {
      message.error(errorText(e));
    }
  }

  return (
    <div>
      {ops.map((op, i) => {
        const expanded = open[op.id] ?? i === 0;
        return (
          <div key={op.id} className="ops-op">
            <div className="ops-op-head" onClick={() => setOpen({ ...open, [op.id]: !expanded })}>
              <Tag color={{ OPEN: "processing", SUSPEND: "warning", RESUME: "success", CLOSE: "error", BACKFILL: "default" }[op.kind]}>{KIND_LABEL[op.kind]}</Tag>
              <b>{op.status === "DONE" ? `完成于 ${dateTime(op.finished_at)}` : op.stuck ? "有步骤失败，自动重试中" : "进行中"}</b>
              <span className="ops-muted">
                {op.requested_by ?? "—"} · {dateTime(op.created_at)}
                {op.reason ? ` · ${op.reason}` : ""}
              </span>
              <span className="ops-grow" />
              {op.status === "RUNNING" && op.stuck && (
                <span onClick={(e) => e.stopPropagation()}>
                  <Tooltip title={admin ? undefined : OBSERVER_TIP}>
                    <Button size="small" icon={<Icon name="refresh" size={14} />} disabled={!admin} onClick={() => void retry(op)}>
                      立即重做
                    </Button>
                  </Tooltip>
                </span>
              )}
              <Icon name={expanded ? "down" : "right"} size={14} style={{ color: "var(--fg-3)" }} />
            </div>
            {expanded && (
              <div className="ops-steps">
                {op.steps.map((s) => {
                  const look = s.status === "FAILED" && !s.rejected ? "WARN" : s.status;
                  return (
                    <div key={s.id} className="ops-step">
                      <span className="ops-step-dot" data-status={look}>
                        <Icon name={s.status === "DONE" ? "check" : s.status === "FAILED" ? "x" : "dot"} size={s.status === "PENDING" ? 10 : 13} />
                      </span>
                      <div className="ops-grow">
                        <div className="ops-row">
                          <SystemIcon code={s.system} size={20} />
                          <b>{systemName(systems, s.system)}</b>
                          <span className="ops-muted">{ACTION_LABEL[s.action] ?? s.action}</span>
                        </div>
                        {s.last_error && (
                          <div className="ops-sub" style={{ color: s.rejected ? "var(--danger)" : "#b45309", marginTop: 3 }}>
                            {s.rejected ? "对方拒绝：" : ""}
                            {s.last_error}
                          </div>
                        )}
                        {s.result?.deleted !== undefined && (
                          <div className="ops-sub" style={{ marginTop: 3 }}>
                            删了{" "}
                            {Object.entries(s.result.deleted as Record<string, number>)
                              .map(([k, n]) => `${k} ${n}`)
                              .join("、") || "0"}
                          </div>
                        )}
                      </div>
                      <span className="ops-sub ops-nw">
                        {s.status === "DONE"
                          ? `完成 ${dateTime(s.finished_at)}`
                          : s.status === "FAILED"
                            ? `第 ${s.attempts} 次失败 · ${dateTime(s.next_attempt_at)} 再试`
                            : s.attempts
                              ? `进行中（已做 ${s.attempts} 批）`
                              : "等前面的步骤"}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
