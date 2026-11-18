"use client";

/**
 * 数字员工（数字员工设计 §3.4）：每个智能体一个，由接入应用自动创建和维护。不能登录、不在组织架构里，
 * 权限只来自直接授给它的角色和岗位 —— 授给部门的对它不生效。这里只能停用 / 启用（紧急止血）。
 */
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Input, Segmented, Table, Tag } from "antd";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { PageHead, PersonAvatar, PersonCell, StatusTag } from "@/components/common";
import { useUserActions } from "@/components/userActions";
import { api } from "@/lib/api";
import { useCan } from "@/lib/session";
import type { User } from "@/lib/types";

interface UserList {
  total: number;
  counts: Record<string, number>;
  items: User[];
}

export default function DigitalPage() {
  const can = useCan();
  const router = useRouter();
  const [status, setStatus] = useState("all");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const actions = useUserActions();
  const list = useQuery({
    queryKey: ["users", "digital", status, q, page],
    queryFn: () => api.get<UserList>("/users", { type: "digital", status, q, page, size: 20 }),
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
  return (
    <>
      <PageHead title="数字员工" />
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 14 }}
        message="每个智能体一个数字员工，由来源应用自动创建和维护。不能登录、不在组织架构里；权限只来自直接授给它的角色和岗位，授给部门的对它不生效。"
      />
      <div className="uc-toolbar">
        <Segmented value={status} onChange={(v) => (setStatus(String(v)), setPage(1))} options={[tab("all", "全部"), tab("active", "正常"), tab("disabled", "已停用")]} />
        <Input.Search allowClear placeholder="搜索名称、账号" style={{ width: 260 }} onSearch={(v) => (setQ(v), setPage(1))} />
      </div>
      <Card styles={{ body: { padding: 0 } }}>
        <Table<User>
          rowKey="id"
          loading={list.isLoading}
          dataSource={list.data?.items ?? []}
          onRow={(u) => ({ onClick: () => router.push(`/digital/${u.id}`), className: "uc-row-click" })}
          pagination={{ current: page, pageSize: 20, total: list.data?.total ?? 0, onChange: setPage, showSizeChanger: false, showTotal: (n) => `共 ${n} 个` }}
          columns={[
            { title: "数字员工", render: (_, u) => <PersonCell digital name={u.name} account={u.account} /> },
            {
              title: "负责人",
              render: (_, u) =>
                u.owner ? (
                  <span className="uc-row">
                    <PersonAvatar name={u.owner.name} size={22} />
                    {u.owner.name}
                    {u.owner.status === "disabled" && <Tag color="warning">负责人已停用</Tag>}
                  </span>
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
                can("user:disable") && (
                  <span onClick={(e) => e.stopPropagation()}>
                    {u.status === "disabled" ? (
                      <Button size="small" onClick={() => void actions.enable(u)}>
                        启用
                      </Button>
                    ) : (
                      <Button size="small" danger onClick={() => actions.disable(u)}>
                        停用
                      </Button>
                    )}
                  </span>
                ),
            },
          ]}
        />
      </Card>
    </>
  );
}
