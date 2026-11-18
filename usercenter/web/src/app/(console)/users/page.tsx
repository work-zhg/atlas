"use client";

/**
 * 人员：本系统自建账号。数字员工单独一个菜单（/digital）—— 原来页内切换容易漏看。
 * 行内只留「编辑」，授权 / 重置密码 / 解锁 / 停用收进「…」；点整行进详情。
 */
import { useQuery } from "@tanstack/react-query";
import { Button, Card, Dropdown, Input, Segmented, Table, Tag } from "antd";
import type { MenuProps } from "antd";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { DeptSelect, PageHead, PersonCell, StatusTag } from "@/components/common";
import { Icon } from "@/components/Icon";
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
  const router = useRouter();
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
  const tab = (key: string, label: string) => ({
    value: key,
    label: (
      <>
        {label} <span className="uc-sub">{counts[key] ?? 0}</span>
      </>
    ),
  });

  const more = (u: User): MenuProps["items"] =>
    [
      can("grant:view") && { key: "grant", icon: <Icon name="shield" size={14} />, label: "授权", onClick: () => router.push(`/perm/grants?subject=user:${u.id}`) },
      can("user:reset_pwd") && u.status !== "disabled" && { key: "reset", icon: <Icon name="key" size={14} />, label: "重置密码", onClick: () => actions.reset(u) },
      can("user:disable") && u.status === "locked" && { key: "unlock", icon: <Icon name="unlock" size={14} />, label: "解锁", onClick: () => void actions.unlock(u) },
      can("user:disable") && { type: "divider" as const },
      can("user:disable") &&
        (u.status === "disabled"
          ? { key: "enable", icon: <Icon name="check" size={14} />, label: "启用", onClick: () => void actions.enable(u) }
          : { key: "disable", icon: <Icon name="ban" size={14} />, label: u.id === me?.user.id ? "停用（不能停用自己）" : "停用", danger: true, disabled: u.id === me?.user.id, onClick: () => actions.disable(u) }),
    ].filter(Boolean) as MenuProps["items"];

  return (
    <>
      <PageHead
        title="人员"
        sub="本系统自建账号，所有接入应用共用。"
        extra={
          can("user:create") && (
            <Button type="primary" icon={<Icon name="userPlus" size={14} />} onClick={() => setForm({ open: true, user: null })}>
              新建用户
            </Button>
          )
        }
      />
      <div className="uc-toolbar">
        <Segmented
          value={status}
          onChange={(v) => (setStatus(String(v)), setPage(1))}
          options={[tab("all", "全部"), tab("active", "正常"), tab("pending", "未激活"), tab("locked", "已锁定"), tab("disabled", "已停用")]}
        />
        <div style={{ width: 220 }}>
          <DeptSelect value={dept} onChange={(v) => (setDept(v as string | undefined), setPage(1))} placeholder="全部部门" />
        </div>
        <Input.Search allowClear placeholder="搜索姓名、账号、邮箱、手机" style={{ width: 260 }} onSearch={(v) => (setQ(v), setPage(1))} />
      </div>
      <Card styles={{ body: { padding: 0 } }}>
        <Table<User>
          rowKey="id"
          loading={list.isLoading}
          dataSource={list.data?.items ?? []}
          onRow={(u) => ({ onClick: () => router.push(`/users/${u.id}`), className: "uc-row-click" })}
          pagination={{ current: page, pageSize: 20, total: list.data?.total ?? 0, onChange: setPage, showSizeChanger: false, showTotal: (n) => `共 ${n} 人` }}
          columns={[
            {
              title: "用户",
              render: (_, u) => <PersonCell name={u.name} account={u.account} extra={u.uc_roles?.map((r) => <Tag key={r} color="purple">{r}</Tag>)} />,
            },
            { title: "部门", render: (_, u) => <span style={{ fontSize: 13 }}>{u.dept_path?.slice(1).join(" / ") || u.dept_path?.[0]}</span> },
            { title: "岗位", render: (_, u) => u.positions?.join("、") || <span className="uc-muted">—</span> },
            {
              title: "状态",
              render: (_, u) => (
                <>
                  <StatusTag status={u.status} />
                  {u.disabled_reason && <div className="uc-sub">{u.disabled_reason}</div>}
                </>
              ),
            },
            { title: "最近登录", render: (_, u) => <span className="uc-muted uc-nw">{u.last_login_at ? fmtTime(u.last_login_at) : "从未登录"}</span> },
            {
              title: "",
              align: "right",
              render: (_, u) => (
                <span className="uc-nw" onClick={(e) => e.stopPropagation()}>
                  {can("user:edit") && (
                    <Button type="text" size="small" onClick={() => setForm({ open: true, user: u })}>
                      编辑
                    </Button>
                  )}
                  {!!more(u)?.length && (
                    <Dropdown trigger={["click"]} menu={{ items: more(u) }}>
                      <Button type="text" size="small" aria-label="更多" icon={<Icon name="more" />} />
                    </Dropdown>
                  )}
                </span>
              ),
            },
          ]}
        />
      </Card>
      <UserForm open={form.open} user={form.user} onClose={() => setForm({ open: false })} />
    </>
  );
}
