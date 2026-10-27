"use client";

import { useQuery } from "@tanstack/react-query";
import { Button, Card, Input, Segmented, Space, Table, Tag } from "antd";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { DeptSelect, PageHead, StatusTag } from "@/components/common";
import { UserForm } from "@/components/UserForm";
import { useUserActions } from "@/components/userActions";
import { api } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useCan, useMe } from "@/lib/session";
import type { User } from "@/lib/types";

interface UserList {
  total: number;
  counts: Record<string, number>;
  items: User[];
}

export default function UsersPage() {
  return (
    <Suspense>
      <UsersInner />
    </Suspense>
  );
}

/**
 * 人和数字员工分开列（数字员工设计 §3.4）：数字员工不进组织树，没有部门；资料由来源应用
 * （如 Atlas）维护，这里只能停用 / 启用。
 */
function UsersInner() {
  const params = useSearchParams();
  const router = useRouter();
  const type = params.get("type") === "digital" ? "digital" : "human";
  return type === "digital" ? (
    <DigitalList onType={() => router.replace("/users")} />
  ) : (
    <HumanList onType={() => router.replace("/users?type=digital")} />
  );
}

function TypeSwitch({ value, onChange }: { value: "human" | "digital"; onChange: () => void }) {
  return (
    <Segmented
      value={value}
      onChange={onChange}
      options={[
        { value: "human", label: "人员" },
        { value: "digital", label: "🤖 数字员工" },
      ]}
    />
  );
}

function HumanList({ onType }: { onType: () => void }) {
  const can = useCan();
  const { data: me } = useMe();
  const [status, setStatus] = useState("all");
  const [dept, setDept] = useState<string>();
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [form, setForm] = useState<{ open: boolean; user?: User | null }>({ open: false });
  const actions = useUserActions();
  const list = useQuery({
    queryKey: ["users", status, dept, q, page],
    queryFn: () => api.get<UserList>("/users", { status, dept_id: dept, q, page, size: 20 }),
  });
  const counts = list.data?.counts ?? {};
  const tab = (key: string, label: string) => ({ value: key, label: `${label} ${counts[key] ?? 0}` });

  return (
    <>
      <PageHead
        title="用户管理"
        sub="本系统自建账号：新建、编辑、停用、重置密码；所有接入应用共用这些账号。"
        extra={can("user:create") && <Button type="primary" onClick={() => setForm({ open: true, user: null })}>＋ 新建用户</Button>}
      />
      <Space style={{ marginBottom: 12, width: "100%" }} wrap>
        <TypeSwitch value="human" onChange={onType} />
        <Segmented
          value={status}
          onChange={(v) => (setStatus(String(v)), setPage(1))}
          options={[tab("all", "全部"), tab("active", "正常"), tab("pending", "未激活"), tab("locked", "已锁定"), tab("disabled", "已停用")]}
        />
        <div style={{ width: 220 }}>
          <DeptSelect value={dept} onChange={(v) => (setDept(v as string | undefined), setPage(1))} placeholder="全部部门" />
        </div>
        <Input.Search allowClear placeholder="搜索姓名 / 账号 / 邮箱 / 手机" style={{ width: 260 }} onSearch={(v) => (setQ(v), setPage(1))} />
      </Space>
      <Card styles={{ body: { padding: 0 } }}>
        <Table<User>
          rowKey="id"
          loading={list.isLoading}
          dataSource={list.data?.items ?? []}
          pagination={{ current: page, pageSize: 20, total: list.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
          columns={[
            {
              title: "用户",
              render: (_, u) => (
                <div>
                  <Link href={`/users/${u.id}`} style={{ fontWeight: 600 }}>
                    {u.name}
                  </Link>{" "}
                  {u.uc_roles?.map((r) => (
                    <Tag key={r} color="purple">
                      {r}
                    </Tag>
                  ))}
                  <div className="uc-muted" style={{ fontSize: 12 }}>
                    {u.account}
                  </div>
                </div>
              ),
            },
            { title: "部门", render: (_, u) => <span style={{ fontSize: 13 }}>{u.dept_path?.slice(1).join(" / ") || u.dept_path?.[0]}</span> },
            { title: "岗位", render: (_, u) => u.positions?.join("、") || <span className="uc-muted">—</span> },
            { title: "状态", render: (_, u) => <StatusTag status={u.status} /> },
            { title: "最近登录", render: (_, u) => <span className="uc-muted">{u.last_login_at ? fmtTime(u.last_login_at) : "从未登录"}</span> },
            {
              title: "",
              align: "right",
              render: (_, u) => (
                <Space size={0}>
                  {can("user:edit") && <Button type="link" size="small" onClick={() => setForm({ open: true, user: u })}>编辑</Button>}
                  {can("user:reset_pwd") && u.status !== "disabled" && <Button type="link" size="small" onClick={() => actions.reset(u)}>重置密码</Button>}
                  {can("user:disable") && u.status === "locked" && <Button type="link" size="small" onClick={() => actions.unlock(u)}>解锁</Button>}
                  {can("user:disable") &&
                    (u.status === "disabled" ? (
                      <Button type="link" size="small" onClick={() => actions.enable(u)}>启用</Button>
                    ) : (
                      <Button type="link" size="small" danger disabled={u.id === me?.user.id} onClick={() => actions.disable(u)}>停用</Button>
                    ))}
                </Space>
              ),
            },
          ]}
        />
      </Card>
      <UserForm open={form.open} user={form.user} onClose={() => setForm({ open: false })} />
    </>
  );
}

function DigitalList({ onType }: { onType: () => void }) {
  const can = useCan();
  const [status, setStatus] = useState("all");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const actions = useUserActions();
  const list = useQuery({
    queryKey: ["users", "digital", status, q, page],
    queryFn: () => api.get<UserList>("/users", { type: "digital", status, q, page, size: 20 }),
  });
  const counts = list.data?.counts ?? {};
  const tab = (key: string, label: string) => ({ value: key, label: `${label} ${counts[key] ?? 0}` });
  return (
    <>
      <PageHead
        title="数字员工"
        sub="每个智能体（Agent）一个，由接入应用自动创建与维护。不能登录、不在组织架构里，权限只来自直接授给它的角色、岗位与数据权限 —— 授给部门的对它不生效。"
      />
      <Space style={{ marginBottom: 12, width: "100%" }} wrap>
        <TypeSwitch value="digital" onChange={onType} />
        <Segmented
          value={status}
          onChange={(v) => (setStatus(String(v)), setPage(1))}
          options={[tab("all", "全部"), tab("active", "正常"), tab("disabled", "已停用")]}
        />
        <Input.Search allowClear placeholder="搜索名称 / 账号" style={{ width: 260 }} onSearch={(v) => (setQ(v), setPage(1))} />
      </Space>
      <Card styles={{ body: { padding: 0 } }}>
        <Table<User>
          rowKey="id"
          loading={list.isLoading}
          dataSource={list.data?.items ?? []}
          pagination={{ current: page, pageSize: 20, total: list.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
          columns={[
            {
              title: "数字员工",
              render: (_, u) => (
                <div>
                  🤖{" "}
                  <Link href={`/users/${u.id}`} style={{ fontWeight: 600 }}>
                    {u.name}
                  </Link>
                  <div className="uc-muted" style={{ fontSize: 12 }}>
                    {u.account}
                  </div>
                </div>
              ),
            },
            {
              title: "负责人",
              render: (_, u) =>
                u.owner ? (
                  <Space size={4}>
                    <Link href={`/users/${u.owner.id}`}>{u.owner.name}</Link>
                    {u.owner.status === "disabled" && <Tag>负责人已停用</Tag>}
                  </Space>
                ) : (
                  "—"
                ),
            },
            { title: "来源应用", render: (_, u) => u.source_app?.name ?? "—" },
            { title: "岗位", render: (_, u) => u.positions?.join("、") || <span className="uc-muted">—</span> },
            { title: "状态", render: (_, u) => <StatusTag status={u.status} /> },
            {
              title: "",
              align: "right",
              render: (_, u) =>
                can("user:disable") &&
                (u.status === "disabled" ? (
                  <Button type="link" size="small" onClick={() => actions.enable(u)}>启用</Button>
                ) : (
                  <Button type="link" size="small" danger onClick={() => actions.disable(u)}>停用</Button>
                )),
            },
          ]}
        />
      </Card>
    </>
  );
}
