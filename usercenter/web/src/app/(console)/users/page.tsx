"use client";

import { useQuery } from "@tanstack/react-query";
import { Button, Card, Input, Segmented, Space, Table, Tag } from "antd";
import Link from "next/link";
import { useState } from "react";

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
