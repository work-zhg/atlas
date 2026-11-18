"use client";

import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Checkbox, Modal, Select, type SelectProps, Space, Tag, TreeSelect, Typography } from "antd";
import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

import { api, BASE_PATH } from "@/lib/api";
import { LEVEL, STATUS } from "@/lib/format";
import { useMe } from "@/lib/session";
import type { Level, UserStatus } from "@/lib/types";

import { Icon, type IconName } from "./Icon";

const STATUS_TAG: Record<UserStatus, string> = { active: "success", pending: "processing", locked: "warning", disabled: "default" };

export function StatusTag({ status }: { status: UserStatus | string }) {
  const s = STATUS[status as UserStatus] ?? { label: status };
  return <Tag color={STATUS_TAG[status as UserStatus] ?? "default"}>{s.label}</Tag>;
}

export function LevelTag({ level }: { level: Level }) {
  const l = LEVEL[level];
  return <Tag color={l.color}>{l.label}</Tag>;
}

/** 页头：标题 + 一句说明 + 右侧主操作（同 Atlas 列表页） */
export function PageHead({ title, sub, extra }: { title: ReactNode; sub?: ReactNode; extra?: ReactNode }) {
  return (
    <div className="uc-page-head">
      <div className="uc-grow">
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
    <div className="uc-page-head" style={{ marginBottom: 8 }}>
      <Button type="text" aria-label="返回" icon={<Icon name="left" />} onClick={() => router.push(back)} />
      {icon}
      <div className="uc-grow">
        <div className="uc-row" style={{ flexWrap: "wrap" }}>
          <h1>{title}</h1>
          {tags}
        </div>
        {sub && <div className="sub">{sub}</div>}
      </div>
      {extra && <Space wrap>{extra}</Space>}
    </div>
  );
}

const AVATAR_COLORS = ["#6366f1", "#0ea5e9", "#f59e0b", "#10b981", "#ec4899", "#8b5cf6", "#14b8a6", "#ef4444"];
const colorOf = (s: string) => AVATAR_COLORS[[...s].reduce((a, c) => a + c.charCodeAt(0), 0) % AVATAR_COLORS.length];

/** 人：圆形头像，取姓名最后一个字（与 Atlas 顶栏一致） */
export function PersonAvatar({ name, size = 30 }: { name: string; size?: number }) {
  return (
    <span className="uc-avatar" style={{ width: size, height: size, fontSize: Math.round(size * 0.42), background: colorOf(name) }} aria-hidden>
      {name.slice(-1)}
    </span>
  );
}

/** 方形图标：部门、数字员工、角色等 */
export function SquareIcon({ icon, size = 36, color }: { icon: IconName; size?: number; color?: string }) {
  return (
    <span
      className="uc-sq"
      style={{ width: size, height: size, borderRadius: Math.round(size * 0.28), ...(color ? { background: `${color}1a`, color } : {}) }}
      aria-hidden
    >
      <Icon name={icon} size={Math.round(size * 0.52)} />
    </span>
  );
}

/** 应用：方形底 + 应用自己的图标（emoji 是应用登记时填的数据） */
export function AppIcon({ app, size = 36 }: { app: { icon: string | null; name: string }; size?: number }) {
  return (
    <span className="uc-sq" style={{ width: size, height: size, borderRadius: Math.round(size * 0.28), fontSize: Math.round(size * 0.5) }} aria-hidden>
      {app.icon || app.name.slice(0, 1)}
    </span>
  );
}

/** 人员单元格：头像 + 姓名（+ 标签）+ 账号 */
export function PersonCell({ name, account, digital, extra }: { name: string; account: string; digital?: boolean; extra?: ReactNode }) {
  return (
    <div className="uc-row" style={{ gap: 10 }}>
      {digital ? <SquareIcon icon="bot" size={30} color="#0ea5e9" /> : <PersonAvatar name={name} />}
      <div style={{ minWidth: 0 }}>
        <div className="uc-row" style={{ gap: 6 }}>
          <b className="uc-nw">{name}</b>
          {extra}
        </div>
        <div className="uc-sub">{account}</div>
      </div>
    </div>
  );
}

/**
 * 临时密码 / Secret：只显示这一次，不可点遮罩关闭。每行可复制，「复制全部」一次带走整段
 * （新建用户时直接发给本人：登录地址、企业代码、账号、临时密码）。
 */
export function showOnce(modal: ReturnType<typeof Modal.useModal>[0], title: string, rows: [string, string][], warn: string) {
  const all = rows.map(([k, v]) => `${k}：${v}`).join("\n");
  modal.info({
    title,
    width: 540,
    icon: null,
    maskClosable: false,
    okText: "我已保存",
    content: (
      <div style={{ marginTop: 8 }}>
        <div className="uc-once">
          {rows.map(([k, v]) => (
            <div key={k} className="uc-once-row">
              <span className="uc-muted">{k}</span>
              <Typography.Text className="uc-mono" style={{ wordBreak: "break-all" }}>
                {v}
              </Typography.Text>
              <Typography.Text copyable={{ text: v, tooltips: [`复制${k}`, "已复制"] }} />
            </div>
          ))}
        </div>
        <Alert type="warning" showIcon style={{ marginTop: 12 }} message={warn} />
        <CopyAll text={all} />
      </div>
    ),
  });
}

function CopyAll({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <Button
      style={{ marginTop: 12 }}
      icon={<Icon name={done ? "check" : "copy"} size={14} />}
      onClick={() => void navigator.clipboard.writeText(text).then(() => setDone(true))}
    >
      {done ? "已复制，可以直接发给对方" : "复制全部"}
    </Button>
  );
}

/** 发给本人的登录信息：登录地址、企业代码（多租户时）、账号、临时密码 */
export function useLoginInfo(): (account: string, temp: string) => [string, string][] {
  const { data: me } = useMe();
  const tenantRequired = useQuery({
    queryKey: ["tenant-required"],
    queryFn: () => api.get<{ required: boolean }>("/auth/tenant-required"),
    staleTime: 300_000,
  });
  return (account, temp) => [
    ["登录地址", typeof window === "undefined" ? `${BASE_PATH}/login` : `${window.location.origin}${BASE_PATH}/login`],
    ...(tenantRequired.data?.required && me ? ([["企业代码", me.tenant.code]] as [string, string][]) : []),
    ["账号", account],
    ["临时密码", temp],
  ];
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
