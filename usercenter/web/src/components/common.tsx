"use client";

import { useQuery } from "@tanstack/react-query";
import { Alert, Checkbox, Modal, Select, type SelectProps, Space, Tag, TreeSelect, Typography } from "antd";
import { useState, type ReactNode } from "react";

import { api } from "@/lib/api";
import { LEVEL, STATUS } from "@/lib/format";
import type { Level, UserStatus } from "@/lib/types";

export function StatusTag({ status }: { status: UserStatus | string }) {
  const s = STATUS[status as UserStatus] ?? { label: status, color: "default" };
  return <Tag color={s.color}>{s.label}</Tag>;
}

export function LevelTag({ level }: { level: Level }) {
  const l = LEVEL[level];
  return <Tag color={l.color}>{l.label}</Tag>;
}

export function PageHead({ title, sub, extra }: { title: string; sub?: ReactNode; extra?: ReactNode }) {
  return (
    <div className="uc-page-head">
      <div>
        <h1>{title}</h1>
        {sub && <div className="sub">{sub}</div>}
      </div>
      <div className="uc-grow" />
      {extra}
    </div>
  );
}

/** 临时密码 / Secret：只显示这一次，不可点遮罩关闭。 */
export function showOnce(modal: ReturnType<typeof Modal.useModal>[0], title: string, rows: [string, string][], warn: string) {
  modal.info({
    title,
    width: 520,
    maskClosable: false,
    okText: "我已保存",
    content: (
      <div>
        {rows.map(([k, v]) => (
          <div key={k} style={{ margin: "8px 0" }}>
            <span className="uc-muted" style={{ display: "inline-block", width: 96 }}>
              {k}
            </span>
            <Typography.Text code copyable>
              {v}
            </Typography.Text>
          </div>
        ))}
        <Alert type="warning" showIcon style={{ marginTop: 12 }} message={warn} />
      </div>
    ),
  });
}

export interface FlatDept {
  id: string;
  parent_id: string | null;
  name: string;
}

export interface DeptTreeNode {
  title: string;
  value: string;
  key: string;
  children: DeptTreeNode[];
}

export function buildDeptTree<T extends FlatDept>(rows: T[], label?: (d: T) => ReactNode): DeptTreeNode[] {
  const nodes = new Map<string, DeptTreeNode & { raw: T }>();
  for (const d of rows) nodes.set(d.id, { title: d.name, value: d.id, key: d.id, children: [], raw: d });
  const roots: DeptTreeNode[] = [];
  for (const d of rows) {
    const n = nodes.get(d.id)!;
    if (label) (n as unknown as { title: ReactNode }).title = label(d);
    const parent = d.parent_id ? nodes.get(d.parent_id) : undefined;
    if (parent) parent.children.push(n);
    else roots.push(n);
  }
  return roots;
}

export function useDirectoryDepts() {
  return useQuery({ queryKey: ["directory", "depts"], queryFn: () => api.get<FlatDept[]>("/directory/depts") });
}

export function DeptSelect({
  value,
  onChange,
  multiple,
  placeholder,
}: {
  value?: string | string[];
  onChange?: (v: string | string[]) => void;
  multiple?: boolean;
  placeholder?: string;
}) {
  const { data = [] } = useDirectoryDepts();
  return (
    <TreeSelect
      value={value}
      onChange={onChange}
      treeData={buildDeptTree(data)}
      treeDefaultExpandAll
      multiple={multiple}
      allowClear
      showSearch
      treeNodeFilterProp="title"
      placeholder={placeholder ?? "选择部门"}
      style={{ width: "100%" }}
    />
  );
}

interface DirUser {
  id: string;
  name: string;
  account: string;
  dept: string | null;
  user_type?: "human" | "digital";
}

/**
 * 选人。digital：连数字员工一起，单独一组（授权时可以选到 Agent 的身份；它们不在组织树里，
 * 数字员工设计 §3.4）。
 */
export function UserSelect({
  value,
  onChange,
  multiple,
  digital,
}: {
  value?: string | string[];
  onChange?: (v: string | string[]) => void;
  multiple?: boolean;
  digital?: boolean;
}) {
  const [q, setQ] = useState("");
  const { data = [], isFetching } = useQuery({
    queryKey: ["directory", "users", q, digital ? "digital" : "human"],
    queryFn: () => api.get<DirUser[]>("/directory/users", { q, limit: 30, digital: digital ? "true" : undefined }),
  });
  const people = data.filter((u) => u.user_type !== "digital");
  const agents = data.filter((u) => u.user_type === "digital");
  const options: SelectProps["options"] = digital
    ? [
        { label: "人员", options: people.map((u) => ({ value: u.id, label: `${u.name}（${u.account}）· ${u.dept}` })) },
        { label: "数字员工", options: agents.map((u) => ({ value: u.id, label: `🤖 ${u.name}（${u.account}）` })) },
      ].filter((g) => g.options.length)
    : people.map((u) => ({ value: u.id, label: `${u.name}（${u.account}）· ${u.dept}` }));
  return (
    <Select
      value={value}
      onChange={onChange}
      mode={multiple ? "multiple" : undefined}
      showSearch
      filterOption={false}
      onSearch={setQ}
      loading={isFetching}
      allowClear
      placeholder={digital ? "搜索姓名或账号（含数字员工）" : "搜索姓名或账号"}
      style={{ width: "100%" }}
      options={options}
    />
  );
}

export interface SubjectValue {
  depts: string[];
  users: string[];
  includeSub: boolean;
}

/** 授权对象：多个部门（可含下级）+ 多个用户。digital：用户里也能选数字员工。 */
export function SubjectPicker({
  value,
  onChange,
  digital,
}: {
  value: SubjectValue;
  onChange: (v: SubjectValue) => void;
  digital?: boolean;
}) {
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <div>
        <div className="uc-muted" style={{ marginBottom: 4 }}>
          组织
        </div>
        <DeptSelect multiple value={value.depts} onChange={(v) => onChange({ ...value, depts: v as string[] })} />
        <Checkbox
          style={{ marginTop: 6 }}
          checked={value.includeSub}
          onChange={(e) => onChange({ ...value, includeSub: e.target.checked })}
        >
          包含下级部门（下级部门的成员也获得）
        </Checkbox>
      </div>
      <div>
        <div className="uc-muted" style={{ marginBottom: 4 }}>
          用户
        </div>
        <UserSelect multiple digital={digital} value={value.users} onChange={(v) => onChange({ ...value, users: v as string[] })} />
      </div>
    </Space>
  );
}

export function toSubjects(v: SubjectValue): { type: "user" | "dept"; id: string }[] {
  return [...v.depts.map((id) => ({ type: "dept" as const, id })), ...v.users.map((id) => ({ type: "user" as const, id }))];
}
