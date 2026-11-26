"use client";

/** 运营中心各页共用的小件：页头、详情页头、方形图标、头像、状态标签、用量条、只显示一次的凭据。
 *  样子与 Atlas、用户中心同一套（agent-platform/doc/prototype/opscenter.html）。 */
import { Alert, Button, Space, Tag, Typography } from "antd";
import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

import { ApiError } from "@/lib/api";
import { KIND_LABEL, bytes, limit } from "@/lib/format";
import type { Tenant } from "@/lib/types";

import { Icon, type IconName } from "./Icon";

/** 页头：标题 + 一句说明 + 右侧主操作（同 Atlas 列表页） */
export function PageHead({ title, sub, extra }: { title: ReactNode; sub?: ReactNode; extra?: ReactNode }) {
  return (
    <div className="ops-page-head">
      <div className="ops-grow">
        <h1>{title}</h1>
        {sub && <div className="sub">{sub}</div>}
      </div>
      {extra}
    </div>
  );
}

/** 详情页头：返回 + 图标 + 名称与标签 + 副标题 + 右侧操作（同 Atlas 详情页） */
export function DetailHead({
  back,
  icon,
  title,
  tags,
  sub,
  extra,
}: {
  back: string;
  icon: ReactNode;
  title: ReactNode;
  tags?: ReactNode;
  sub?: ReactNode;
  extra?: ReactNode;
}) {
  const router = useRouter();
  return (
    <div className="ops-page-head" style={{ marginBottom: 8 }}>
      <Button type="text" aria-label="返回" icon={<Icon name="left" />} onClick={() => router.push(back)} />
      {icon}
      <div className="ops-grow">
        <div className="ops-row" style={{ flexWrap: "wrap" }}>
          <h1>{title}</h1>
          {tags}
        </div>
        {sub && <div className="sub">{sub}</div>}
      </div>
      {extra && <Space wrap>{extra}</Space>}
    </div>
  );
}

export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  return <Alert type="error" showIcon message={errorText(error)} style={{ marginBottom: 16 }} />;
}

const COLORS = ["#6366f1", "#0ea5e9", "#f59e0b", "#10b981", "#ec4899", "#8b5cf6", "#14b8a6", "#ef4444"];
export const colorOf = (s: string) => COLORS[[...s].reduce((a, c) => a + c.charCodeAt(0), 0) % COLORS.length]!;

/** 人：圆形头像，取名字最后一个字（与 Atlas 顶栏一致） */
/** 头像字：中文名取最后一个字，英文账号取首字母 */
export function PersonAvatar({ name, size = 30 }: { name: string; size?: number }) {
  const latin = /^[\x00-\x7f]+$/.test(name);
  return (
    <span className="ops-avatar" style={{ width: size, height: size, fontSize: Math.round(size * 0.42), background: colorOf(name) }} aria-hidden>
      {latin ? name.charAt(0).toUpperCase() : name.slice(-1)}
    </span>
  );
}

/** 方形图标：租户、系统、技能等 */
export function SquareIcon({ icon, size = 36, color }: { icon: IconName; size?: number; color?: string }) {
  return (
    <span
      className="ops-sq"
      style={{ width: size, height: size, borderRadius: Math.round(size * 0.28), ...(color ? { background: `${color}1a`, color } : {}) }}
      aria-hidden
    >
      <Icon name={icon} size={Math.round(size * 0.52)} />
    </span>
  );
}

/** 应用：方形底 + 应用自己的图标（emoji 是应用接入时填的数据） */
export function AppIcon({ app, size = 36 }: { app: { icon: string | null; name: string }; size?: number }) {
  return (
    <span className="ops-sq" style={{ width: size, height: size, borderRadius: Math.round(size * 0.28), fontSize: Math.round(size * 0.5) }} aria-hidden>
      {app.icon || app.name.slice(0, 1)}
    </span>
  );
}

/** 各系统的标识（登记的系统编码 → 图标、颜色）；没登记样子的用通用的「系统」 */
const SYSTEM_LOOK: Record<string, { icon: IconName; color: string }> = {
  usercenter: { icon: "users", color: "#6366f1" },
  atlas: { icon: "spark", color: "#4f46e5" },
  knowledge: { icon: "book", color: "#0ea5e9" },
  skill: { icon: "zap", color: "#f59e0b" },
  mcpgateway: { icon: "plug", color: "#10b981" },
  modelgateway: { icon: "layers", color: "#8b5cf6" },
  memory: { icon: "brain", color: "#ec4899" },
};
export function SystemIcon({ code, size = 28 }: { code: string; size?: number }) {
  const look = SYSTEM_LOOK[code] ?? { icon: "server", color: colorOf(code) };
  return <SquareIcon icon={look.icon} color={look.color} size={size} />;
}

export function TenantIcon({ tenant, size = 32 }: { tenant: Pick<Tenant, "code" | "status">; size?: number }) {
  return <SquareIcon icon="building" color={tenant.status === "closed" ? "#94a3b8" : colorOf(tenant.code)} size={size} />;
}

/** 租户状态：正常 / 已到期 / 已停用 / 已关闭，再加进行中的操作 */
export function TenantTags({ tenant }: { tenant: Tenant }) {
  const op = tenant.operation;
  return (
    <>
      {tenant.status === "active" && (tenant.usable ? <Tag color="success">正常</Tag> : <Tag color="error">已到期</Tag>)}
      {tenant.status === "suspended" && <Tag color="warning">已停用</Tag>}
      {tenant.status === "closed" && <Tag>已关闭</Tag>}
      {op && (
        <Tag color={op.stuck ? "error" : "processing"} icon={<Icon name={op.stuck ? "alert" : "refresh"} size={12} style={{ marginRight: 4, verticalAlign: -2 }} />}>
          {KIND_LABEL[op.kind]}
          {op.stuck ? "：有步骤失败" : "中"}
        </Tag>
      )}
    </>
  );
}

/** 离到期还有几天（负数 = 已过期）；不到期为 null */
export function daysLeft(iso: string | null): number | null {
  if (!iso) return null;
  return Math.floor((new Date(iso).getTime() - Date.now()) / 86_400_000);
}

/** 到期：日期 + 还有几天 / 已过期几天（快到期、已过期标颜色） */
export function Expiry({ tenant }: { tenant: Tenant }) {
  if (tenant.status === "closed") return <span className="ops-muted">—</span>;
  if (!tenant.expires_at) return <span className="ops-muted">不到期</span>;
  const day = tenant.expires_at.slice(0, 10);
  const d = daysLeft(tenant.expires_at)!;
  if (tenant.status !== "active") return <span className="ops-muted ops-nw">{day}</span>;
  if (d < 0)
    return (
      <span style={{ color: "var(--danger)" }}>
        <span className="ops-nw">{day}</span>
        <div className="ops-sub ops-nw" style={{ color: "var(--danger)" }}>
          已过期 {-d} 天
        </div>
      </span>
    );
  if (d <= 30)
    return (
      <span>
        <span className="ops-nw">{day}</span>
        <div className="ops-sub ops-nw" style={{ color: "#b45309" }}>
          还有 {d} 天
        </div>
      </span>
    );
  return <span className="ops-nw">{day}</span>;
}

/** 人数 / 名额：进度条（90% 以上变橙） */
export function Seats({ tenant }: { tenant: Tenant }) {
  if (tenant.status === "closed") return <span className="ops-muted">—</span>;
  const users = tenant.users ?? 0;
  if (!tenant.max_users)
    return (
      <span className="ops-nw">
        {users} 人 <span className="ops-sub">· 不限</span>
      </span>
    );
  const p = Math.min(100, Math.round((users * 100) / tenant.max_users));
  return (
    <span className="ops-meter">
      <span className="bar">
        <i data-warn={p >= 90} style={{ width: `${p}%` }} />
      </span>
      <span className="ops-nw">
        {users} / {tenant.max_users}
      </span>
    </span>
  );
}

export const QUOTA_LABEL: Record<string, string> = { running_runs: "同时运行的对话", sandboxes: "沙箱", storage_bytes: "存储" };
export const quotaText = (field: string, v: number | null | undefined) => (field === "storage_bytes" ? bytes(v) : limit(v));

/** 用量 / 份额：一行文字 + 进度条 */
export function UsageBar({ label, field, used, cap }: { label: ReactNode; field: string; used: number; cap: number | null | undefined }) {
  const p = cap ? Math.min(100, Math.round((used * 100) / cap)) : 0;
  return (
    <div className="ops-ubar">
      <div className="ops-row">
        <span>{label}</span>
        <span className="ops-grow" />
        <span className="ops-nw">
          <b>{quotaText(field, used)}</b>
          <span className="ops-muted"> / {quotaText(field, cap)}</span>
        </span>
      </div>
      <div className="track">
        <i data-warn={p >= 90} style={{ width: `${cap ? p : 0}%` }} />
      </div>
    </div>
  );
}

export function ResultTag({ result }: { result: string | null }) {
  if (result === "OK") return <Tag color="success">成功</Tag>;
  if (result === "FAILED") return <Tag color="error">失败</Tag>;
  return <Tag color="processing">进行中</Tag>;
}

/**
 * 临时密码 / 新凭据：只显示这一次。每行可复制，「复制全部」一次带走整段（开通租户时直接发给企业管理员）。
 */
export function OnceBox({ rows, warn }: { rows: [string, string][]; warn: string }) {
  return (
    <div>
      <Alert type="warning" showIcon message={warn} style={{ marginBottom: 10 }} />
      <div className="ops-once">
        {rows.map(([k, v]) => (
          <div key={k} className="ops-once-row">
            <span className="ops-muted">{k}</span>
            <Typography.Text className="ops-mono" style={{ wordBreak: "break-all" }}>
              {v}
            </Typography.Text>
            <Typography.Text copyable={{ text: v, tooltips: [`复制${k}`, "已复制"] }} />
          </div>
        ))}
      </div>
    </div>
  );
}

export function CopyAll({ rows }: { rows: [string, string][] }) {
  const [done, setDone] = useState(false);
  const text = rows.map(([k, v]) => `${k}：${v}`).join("\n");
  return (
    <Button icon={<Icon name={done ? "check" : "copy"} size={14} />} onClick={() => void navigator.clipboard.writeText(text).then(() => setDone(true))}>
      {done ? "已复制" : "复制全部"}
    </Button>
  );
}

/** 兼容旧用法：只有一项凭据 */
export function OneTimeSecret({ label, value }: { label: string; value: string }) {
  return <OnceBox rows={[[label, value]]} warn={`${label}只显示这一次，关掉就再也看不到`} />;
}

/** 观察员：按钮照样显示但置灰，悬停说明为什么（不直接藏掉，免得以为没有这个功能） */
/** 级别不够时按钮置灰的提示（不隐藏：让人知道有这个操作、去找系统租户的管理员要权限） */
export const OBSERVER_TIP = "你在这个模块只能查看；要改请找系统租户的管理员在用户中心授「读写」";
export const OWNER_TIP = "这个操作不可恢复，要这个模块的 Owner 权限";
