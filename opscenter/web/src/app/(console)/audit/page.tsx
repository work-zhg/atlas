"use client";

/**
 * 运营审计：谁在什么时候做了什么。只追加，不记任何密码、临时密码、密钥。
 * 按类别筛 = 后端的 action 前缀；「操作」列写中文说法，下面小字是原始编码，方便对日志。
 */
import { useInfiniteQuery } from "@tanstack/react-query";
import { Button, Drawer, Input, Segmented, Table } from "antd";
import Link from "next/link";
import { useMemo, useState } from "react";

import { ErrorBox, PageHead, PersonAvatar, ResultTag } from "@/components/common";
import { Icon } from "@/components/Icon";
import { api } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { systemName, useSystems } from "@/lib/session";
import type { AuditEntry, SystemInfo } from "@/lib/types";

const PAGE = 50;

/** 类别 → action 前缀（后端按前缀筛） */
const CATEGORIES: { value: string; label: string }[] = [
  { value: "", label: "全部" },
  { value: "tenant.", label: "租户" },
  { value: "system.", label: "系统与应用目录" },
  { value: "operator.", label: "运营账号" },
  { value: "login.", label: "登录" },
];

const LABEL: Record<string, string> = {
  "login.ok": "登录",
  "login.failed": "登录失败",
  "login.locked": "登录失败，账号已锁定",
  "operator.create": "开运营账号",
  "operator.update": "修改运营账号",
  "operator.enable": "启用运营账号",
  "operator.disable": "停用运营账号",
  "operator.reset_password": "重置运营账号的密码",
  "operator.change_password": "修改自己的密码",
  "tenant.open": "开通租户",
  "tenant.suspend": "停用租户",
  "tenant.resume": "恢复租户",
  "tenant.close": "关闭租户",
  "tenant.update": "修改租户",
  "tenant.retry": "立即重做失败的步骤",
  "tenant.reissue_admin_password": "补发首个管理员的临时密码",
};
const METHOD: Record<string, string> = { post: "提交", put: "修改", patch: "修改", delete: "删除" };

/** 只有这几种跨系统的操作做完后补记结果；其余的记下时就已经有结论（没写 result） */
const LIFECYCLE = new Set(["tenant.open", "tenant.suspend", "tenant.resume", "tenant.close"]);
const FAILED = new Set(["login.failed", "login.locked"]);

/** OK / FAILED；null = 跨系统的操作还在做 */
function outcome(e: AuditEntry): string | null {
  if (e.result) return e.result;
  if (LIFECYCLE.has(e.action)) return null;
  return FAILED.has(e.action) ? "FAILED" : "OK";
}

/** 系统代理的写操作：target 是「系统编码 路径」 */
function describe(e: AuditEntry, systems: SystemInfo[] | undefined): { label: string; target: string } {
  const m = e.action.match(/^system\.(\w+)$/);
  if (m) {
    const [code, ...rest] = (e.target ?? "").split(" ");
    return { label: `${systemName(systems, code!)} · ${METHOD[m[1]!] ?? m[1]}`, target: rest.join(" ") };
  }
  return { label: LABEL[e.action] ?? e.action, target: e.target ?? "" };
}

export default function AuditPage() {
  const [action, setAction] = useState("");
  const [kw, setKw] = useState("");
  const [open, setOpen] = useState<AuditEntry | null>(null);
  const { data: systems } = useSystems();
  const q = useInfiniteQuery({
    queryKey: ["audit", action],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => api.get<AuditEntry[]>("/audit", { action, before: pageParam, limit: PAGE }),
    getNextPageParam: (last) => (last.length === PAGE ? last[last.length - 1]?.at : undefined),
  });
  const rows = useMemo(() => {
    const k = kw.trim();
    const all = q.data?.pages.flat() ?? [];
    if (!k) return all;
    return all.filter((e) => {
      const d = describe(e, systems);
      return [e.action, e.operator ?? "", d.label, d.target].some((s) => s.includes(k));
    });
  }, [q.data, kw, systems]);

  return (
    <>
      <PageHead title="运营审计" sub="每个写操作一条，登录也记；跨系统的操作（开通、停用、关闭）做完后补记结果。只能看，不能改" />
      <div className="ops-toolbar">
        <Segmented value={action} onChange={(v) => setAction(v as string)} options={CATEGORIES} />
        <Input allowClear prefix={<Icon name="search" size={14} />} placeholder="在已加载的记录里搜" style={{ width: 240 }} value={kw} onChange={(e) => setKw(e.target.value)} />
      </div>
      <ErrorBox error={q.error} />
      <Table<AuditEntry>
        rowKey="id"
        loading={q.isLoading}
        dataSource={rows}
        pagination={false}
        onRow={(e) => ({ onClick: () => setOpen(e), className: "ops-row-click" })}
        locale={{ emptyText: "没有记录" }}
        columns={[
          { title: "时间", dataIndex: "at", width: 180, render: (v: string) => <span className="ops-muted ops-nw">{dateTime(v)}</span> },
          {
            title: "运营人员",
            dataIndex: "operator",
            render: (v: string | null) =>
              v ? (
                <span className="ops-row" style={{ gap: 8 }}>
                  <PersonAvatar name={v} size={24} />
                  <span className="ops-nw">{v}</span>
                </span>
              ) : (
                <span className="ops-muted">—</span>
              ),
          },
          {
            title: "操作",
            render: (_, e) => (
              <div>
                <div>{describe(e, systems).label}</div>
                <div className="ops-sub ops-mono">{e.action}</div>
              </div>
            ),
          },
          {
            title: "对象",
            render: (_, e) => {
              const t = describe(e, systems).target || "—";
              return e.tenant_id ? (
                <Link href={`/tenants/${e.tenant_id}`} onClick={(ev) => ev.stopPropagation()}>
                  {t}
                </Link>
              ) : (
                <span className="ops-mono" style={{ wordBreak: "break-all" }}>
                  {t}
                </span>
              );
            },
          },
          { title: "结果", width: 90, render: (_, e) => <ResultTag result={outcome(e)} /> },
        ]}
      />
      {q.hasNextPage && (
        <div style={{ textAlign: "center", marginTop: 12 }}>
          <Button loading={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()}>
            加载更早的
          </Button>
        </div>
      )}
      {open && <Detail entry={open} systems={systems} onClose={() => setOpen(null)} />}
    </>
  );
}

function Detail({ entry: e, systems, onClose }: { entry: AuditEntry; systems: SystemInfo[] | undefined; onClose: () => void }) {
  const d = describe(e, systems);
  return (
    <Drawer
      open
      width={560}
      onClose={onClose}
      title={
        <div className="ops-row">
          {d.label}
          <ResultTag result={outcome(e)} />
        </div>
      }
    >
      <dl className="ops-kv">
        <dt>时间</dt>
        <dd>{dateTime(e.at)}</dd>
        <dt>运营人员</dt>
        <dd>
          {e.operator ? (
            <>
              <PersonAvatar name={e.operator} size={22} />
              {e.operator}
            </>
          ) : (
            "—"
          )}
        </dd>
        <dt>操作</dt>
        <dd className="ops-mono">{e.action}</dd>
        <dt>对象</dt>
        <dd>{e.tenant_id ? <Link href={`/tenants/${e.tenant_id}`}>{d.target || "—"}</Link> : <span className="ops-mono">{d.target || "—"}</span>}</dd>
        <dt>结果</dt>
        <dd>
          {LIFECYCLE.has(e.action) ? (e.finished_at ? `做完于 ${dateTime(e.finished_at)}` : "各系统还在做，做完后补记") : <ResultTag result={outcome(e)} />}
        </dd>
      </dl>
      <div className="ops-subhead">参数摘要</div>
      {e.detail ? <pre className="ops-json">{JSON.stringify(e.detail, null, 2)}</pre> : <span className="ops-muted">没有参数</span>}
      <div className="ops-sub" style={{ marginTop: 8 }}>
        密码、临时密码、密钥不进审计
      </div>
    </Drawer>
  );
}
