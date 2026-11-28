"use client";

/** 各页共用的小件：页面外框、状态、人和数字员工、文件、加载与出错。 */
import { Button, Result, Spin } from "antd";
import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";

import { Icon, type IconName } from "@atlas/chat-ui/src/components/Icon";

import { ApiError, errorText } from "@/lib/api";
import { base, NODE_STATUS, PROCESS_STATUS } from "@/lib/format";
import { useNames } from "@/lib/queries";
import type { NodeStatus, ProcessStatus } from "@/lib/types";

export { Icon, type IconName };

const COLORS = ["#6366f1", "#0ea5e9", "#f59e0b", "#10b981", "#ec4899", "#8b5cf6", "#14b8a6", "#ef4444"];
export function colorOf(key: string): string {
  let h = 0;
  for (const c of key) h = (h * 31 + c.charCodeAt(0)) | 0;
  return COLORS[Math.abs(h) % COLORS.length]!;
}

/** 普通页面：内容区自己滚动、居中、留白 */
export function Page({ children, narrow, style }: { children: ReactNode; narrow?: boolean; style?: CSSProperties }) {
  return (
    <div className="tf-scroll">
      <div className={`tf-page ${narrow ? "narrow" : ""}`} style={style}>
        {children}
      </div>
    </div>
  );
}

export function Crumb({ items }: { items: (readonly [string, string | undefined])[] }) {
  return (
    <div className="tf-crumb">
      {items.map(([label, href], i) => (
        <span key={i}>
          {i > 0 && " / "}
          {href ? <Link href={href}>{label}</Link> : label}
        </span>
      ))}
    </div>
  );
}

export function PageHead({ title, sub, extra }: { title: ReactNode; sub?: ReactNode; extra?: ReactNode }) {
  return (
    <div className="tf-head">
      <div className="grow">
        <h1 className="row">{title}</h1>
        {sub && <div className="sub">{sub}</div>}
      </div>
      {extra && <div className="row">{extra}</div>}
    </div>
  );
}

export function NodeStatusTag({ status }: { status: NodeStatus }) {
  const s = NODE_STATUS[status];
  return (
    <span className="st" style={{ color: s.color, background: s.bg }} title={s.hint}>
      {s.label}
    </span>
  );
}

/** 步骤序号：第几步（同时进行的步骤同号），颜色跟状态走 */
export function StepNo({ n, status }: { n: number | undefined; status?: NodeStatus }) {
  const c = status && status !== "pending" ? NODE_STATUS[status] : null;
  return (
    <span className="stepno" style={c ? { color: c.color, background: c.bg } : undefined}>
      {n ?? "·"}
    </span>
  );
}

export function ProcessStatusTag({ status }: { status: ProcessStatus }) {
  const map = { running: ["#0369a1", "#e6f6fe"], completed: ["#16a34a", "#e7f6ec"], terminated: ["#64748b", "#f1f3f7"] } as const;
  const [c, bg] = map[status];
  return (
    <span className="st" style={{ color: c, background: bg }}>
      {PROCESS_STATUS[status].label}
    </span>
  );
}

/** 节点进度条：每个节点一格，颜色是状态 */
export function ProgressBar({ nodes }: { nodes: { id: string; name: string; status: NodeStatus }[] }) {
  const fill: Partial<Record<NodeStatus, string>> = {
    working: "#38bdf8",
    exit_review: "#f59e0b",
    exited: "#2dd4bf",
    admit_review: "#a855f7",
    passed: "#22c55e",
    returned: "#ef4444",
  };
  return (
    <span className="pbar">
      {nodes.map((n) => (
        <i key={n.id} title={`${n.name} · ${NODE_STATUS[n.status].label}`} style={fill[n.status] ? { background: fill[n.status] } : undefined} />
      ))}
    </span>
  );
}

export function Avatar({ name, id, size = 20, bot }: { name: string; id?: string; size?: number; bot?: boolean }) {
  return (
    <span
      className={`av ${bot ? "bot" : ""}`}
      style={{ width: size, height: size, fontSize: size * 0.45, background: colorOf(id ?? name) }}
      aria-hidden
    >
      {bot ? <Icon name="bot" size={Math.round(size * 0.62)} /> : (name || "?").slice(0, 1)}
    </span>
  );
}

/** 一批人名：给 id 拿名字。用户中心查不到的显示 id 前几位 */
export function useNameOf(ids: (string | null | undefined)[]): (id: string | null | undefined) => string {
  const { data } = useNames(ids);
  return (id) => (id ? (data?.[id]?.name ?? `${id.slice(0, 6)}…`) : "—");
}

export function Who({ id, name, size = 18 }: { id: string; name: string; size?: number }) {
  return (
    <span className="who">
      <Avatar id={id} name={name} size={size} />
      <span>{name}</span>
    </span>
  );
}

export function AgentChip({ name, size = 18 }: { name: string; size?: number }) {
  return (
    <span className="who">
      <Avatar name={name} size={size} bot />
      <span>{name}</span>
    </span>
  );
}

/** 流程文件的预览页（新标签页打开）。不经应用外壳：整页都给文件 */
export const viewHref = (processId: string, path: string) =>
  `/view?flow=${encodeURIComponent(processId)}&path=${encodeURIComponent(path)}`;

/** 一个文件：点了在新标签页打开预览 */
export function FileChip({ processId, path }: { processId: string; path: string }) {
  return (
    <Link className="inchip" title={`${path}（在新标签页打开）`} href={viewHref(processId, path)} target="_blank" rel="noopener">
      <Icon name="file" size={13} />
      <span className="ellipsis">{base(path)}</span>
    </Link>
  );
}

export function Loading() {
  return <Spin style={{ display: "block", margin: "100px auto" }} />;
}

/** 加载失败：401 由外层带去登录；403 / 404 说看不到；其余可重试 */
export function LoadError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const status = error instanceof ApiError ? error.status : 0;
  if (status === 403 || status === 404)
    return <Result status="404" title="看不到这个页面" subTitle={errorText(error)} style={{ marginTop: 60 }} />;
  return (
    <Result
      status="warning"
      title={status >= 500 || !status ? "服务暂时不可用" : "加载失败"}
      subTitle={status >= 500 || !status ? "后端没有响应，请稍后重试" : errorText(error)}
      extra={onRetry && <Button onClick={onRetry}>重试</Button>}
      style={{ marginTop: 60 }}
    />
  );
}

export function Empty({ icon = "layers", title, children }: { icon?: IconName; title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <Icon name={icon} size={28} />
      <h3>{title}</h3>
      {children && <div className="sm">{children}</div>}
    </div>
  );
}
