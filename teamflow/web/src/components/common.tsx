"use client";

import { useQuery } from "@tanstack/react-query";
import { Avatar, Empty, Select, Space, Tag, Tooltip, TreeSelect } from "antd";
import { useState, type ReactNode } from "react";

import { api } from "@/lib/api";
import { LEVEL } from "@/lib/format";
import type { DirDept, DirUser, Level } from "@/lib/types";

export function PageHead({ title, sub, extra }: { title: ReactNode; sub?: ReactNode; extra?: ReactNode }) {
  return (
    <div className="tf-page-head">
      <div>
        <h1>{title}</h1>
        {sub && <div className="sub">{sub}</div>}
      </div>
      <div className="tf-grow" />
      {extra}
    </div>
  );
}

export function LevelTag({ level }: { level: Level }) {
  const l = LEVEL[level];
  return <Tag color={l.color}>{l.label}</Tag>;
}

export function StatusTag({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    active: ["启用", "green"],
    disabled: ["已停用", "default"],
    archived: ["已归档", "default"],
  };
  const [label, color] = map[status] ?? [status, "default"];
  return <Tag color={color}>{label}</Tag>;
}

export function AgentAvatar({ name, available = true, size = 28 }: { name: string; available?: boolean; size?: number }) {
  return (
    <Tooltip title={available ? name : `${name}（不可用）`}>
      <Avatar size={size} style={{ background: available ? "#0ea5e9" : "#cbd5e1", fontSize: size * 0.45 }}>
        🤖
      </Avatar>
    </Tooltip>
  );
}

export function PersonAvatar({ name, size = 28 }: { name: string; size?: number }) {
  return (
    <Avatar size={size} style={{ background: "#6366f1", fontSize: size * 0.45 }}>
      {name.slice(-1)}
    </Avatar>
  );
}

export function EmptyBox({ text, children }: { text: string; children?: ReactNode }) {
  return (
    <Empty description={text} style={{ padding: "36px 0" }}>
      {children}
    </Empty>
  );
}

/** 选人：代理用户中心开放接口，只返回 TeamFlow 可访问范围内的人。 */
export function UserPicker({
  value,
  onChange,
  multiple = true,
  placeholder = "搜索姓名或账号",
  options: limit,
}: {
  value?: string[];
  onChange?: (v: string[]) => void;
  multiple?: boolean;
  placeholder?: string;
  /** 只能在这些人里选（如团队成员）；不传则搜索全部 */
  options?: { id: string; name: string; account: string }[];
}) {
  const [q, setQ] = useState("");
  const { data = [] } = useQuery({
    queryKey: ["dir-users", q],
    queryFn: () => api.get<DirUser[]>("/directory/users", { q }),
    enabled: !limit,
  });
  const source = limit ?? data;
  return (
    <Select
      mode={multiple ? "multiple" : undefined}
      showSearch
      allowClear
      value={value}
      onChange={(v) => onChange?.(Array.isArray(v) ? v : v ? [v] : [])}
      placeholder={placeholder}
      filterOption={limit ? (input, opt) => String(opt?.label ?? "").includes(input) : false}
      onSearch={limit ? undefined : setQ}
      options={source.map((u) => ({ value: u.id, label: `${u.name}（${u.account}）` }))}
      style={{ width: "100%" }}
    />
  );
}

export function DeptPicker({ value, onChange }: { value?: string[]; onChange?: (v: string[]) => void }) {
  const { data = [] } = useQuery({ queryKey: ["dir-depts"], queryFn: () => api.get<DirDept[]>("/directory/depts") });
  type N = { title: string; value: string; key: string; children: N[] };
  const nodes = new Map<string, N>(data.map((d) => [d.id, { title: d.name, value: d.id, key: d.id, children: [] }]));
  const roots: N[] = [];
  for (const d of data) {
    const n = nodes.get(d.id)!;
    const p = d.parent_id ? nodes.get(d.parent_id) : undefined;
    (p ? p.children : roots).push(n);
  }
  return (
    <TreeSelect
      multiple
      treeDefaultExpandAll
      value={value}
      onChange={onChange}
      treeData={roots}
      placeholder="选择部门"
      style={{ width: "100%" }}
    />
  );
}

export function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <Space direction="vertical" size={0}>
      <span className="tf-muted" style={{ fontSize: 12 }}>
        {label}
      </span>
      <span style={{ fontWeight: 650, fontSize: 15 }}>{value}</span>
    </Space>
  );
}
