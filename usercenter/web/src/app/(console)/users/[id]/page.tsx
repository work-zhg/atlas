"use client";

import { useQuery } from "@tanstack/react-query";
import { Avatar, Button, Card, Descriptions, Empty, Space, Table, Tabs, Tag } from "antd";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { LevelTag, StatusTag } from "@/components/common";
import { UserForm } from "@/components/UserForm";
import { useUserActions } from "@/components/userActions";
import { api } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useCan, useMe } from "@/lib/session";
import type { AppInfo, GrantRow, Level, MenuNode, User } from "@/lib/types";


interface Effective {
  roles: { id: string; name: string; code: string; app: string; sources: string[] }[];
  apps: { app: AppInfo; can_access: boolean; roles: string[]; permissions: string[]; menus: MenuNode[] }[];
  data: { id: string; data_code: string; data_name: string; level: Level }[];
  grants: { own: GrantRow[]; inherited: GrantRow[] };
}

function menuNames(nodes: MenuNode[]): string[] {
  return nodes.flatMap((n) => (n.children ? menuNames(n.children) : [n.name]));
}

export default function UserDetailPage() {
  const { id } = useParams<{ id: string }>();
  const can = useCan();
  const router = useRouter();
  const { data: me } = useMe();
  const actions = useUserActions();
  const [edit, setEdit] = useState(false);
  const user = useQuery({ queryKey: ["user", id], queryFn: () => api.get<User>(`/users/${id}`) });
  const logs = useQuery({
    queryKey: ["user", id, "logs"],
    queryFn: () => api.get<{ time: string; ip: string | null; ok: boolean; reason: string | null }[]>(`/users/${id}/login-logs`),
  });
  const apps = useQuery({ queryKey: ["user", id, "apps"], queryFn: () => api.get<AppInfo[]>(`/users/${id}/apps`) });
  const eff = useQuery({
    queryKey: ["user", id, "effective"],
    queryFn: () => api.get<Effective>(`/users/${id}/effective`),
    enabled: can("grant:view"),
  });
  const u = user.data;
  if (!u) return <Card loading />;

  const grants = eff.data ? [...eff.data.grants.own, ...eff.data.grants.inherited] : [];
  return (
    <>
      <div style={{ display: "flex", marginBottom: 12 }}>
        <div className="uc-grow" />
        <Button onClick={() => router.push("/users")}>← 返回用户列表</Button>
      </div>
      <Card>
        <div style={{ display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap" }}>
          <Avatar size={56} style={{ background: "#6366f1", fontSize: 22 }}>
            {u.name.slice(-1)}
          </Avatar>
          <div>
            <Space>
              <span style={{ fontSize: 20, fontWeight: 650 }}>{u.name}</span>
              <StatusTag status={u.status} />
              {u.must_change_password && <Tag color="gold">须改密</Tag>}
              {u.uc_roles?.map((r) => <Tag key={r} color="purple">{r}</Tag>)}
            </Space>
            <div className="uc-muted" style={{ marginTop: 4 }}>
              {u.account} · {u.dept_path?.join(" / ")} {u.positions?.length ? `· ${u.positions.join("、")}` : ""}
            </div>
          </div>
          <div className="uc-grow" />
          <Space>
            {can("user:edit") && <Button onClick={() => setEdit(true)}>编辑</Button>}
            {can("user:reset_pwd") && u.status !== "disabled" && <Button onClick={() => actions.reset(u)}>重置密码</Button>}
            {can("user:disable") && u.status === "locked" && <Button onClick={() => actions.unlock(u)}>解锁</Button>}
            {can("user:disable") &&
              (u.status === "disabled" ? (
                <Button onClick={() => actions.enable(u)}>启用</Button>
              ) : (
                <Button danger disabled={u.id === me?.user.id} onClick={() => actions.disable(u)}>停用</Button>
              ))}
          </Space>
        </div>
        <Tabs
          style={{ marginTop: 12 }}
          items={[
            {
              key: "info",
              label: "基本信息",
              children: (
                <Descriptions column={1} size="small" styles={{ label: { width: 110 } }}>
                  <Descriptions.Item label="邮箱">{u.email}</Descriptions.Item>
                  <Descriptions.Item label="手机">{u.phone || "—"}</Descriptions.Item>
                  <Descriptions.Item label="部门">{u.dept_path?.join(" / ")}</Descriptions.Item>
                  <Descriptions.Item label="岗位">
                    {u.positions?.join("、") || "—"} <span className="uc-muted">（在权限管理中授权）</span>
                  </Descriptions.Item>
                  <Descriptions.Item label="直属上级">
                    {u.manager ? (
                      <Space>
                        <Link href={`/users/${u.manager.id}`}>{u.manager.name}</Link>
                        <span className="uc-muted">（{u.manager.source === "own_dept" ? "所在部门负责人" : "上级部门负责人"}，由组织架构计算）</span>
                      </Space>
                    ) : (
                      "—"
                    )}
                  </Descriptions.Item>
                  {u.disabled_reason && <Descriptions.Item label="停用原因">{u.disabled_reason}</Descriptions.Item>}
                  <Descriptions.Item label="创建时间">{fmtTime(u.created_at)}</Descriptions.Item>
                  <Descriptions.Item label="最近登录">{u.last_login_at ? fmtTime(u.last_login_at) : "从未登录"}</Descriptions.Item>
                </Descriptions>
              ),
            },
            ...(can("grant:view")
              ? [
                  {
                    key: "perm",
                    label: "权限",
                    children: eff.data ? (
                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, alignItems: "start" }}>
                        <Space direction="vertical" style={{ width: "100%" }} size={16}>
                          <Card size="small" title={`生效的授权（${grants.length}）`} extra={<Link href={`/grants?subject=user:${u.id}`}>在权限管理中授权 →</Link>}>
                            {grants.length ? (
                              grants.map((g) => (
                                <div key={g.id} style={{ display: "flex", justifyContent: "space-between", padding: "4px 0" }}>
                                  <span>
                                    <Tag color={g.kind === "position" ? "blue" : "purple"}>{g.kind === "position" ? "岗位" : "角色"}</Tag>
                                    {g.target.name}
                                  </span>
                                  <span className="uc-muted">
                                    {g.subject.type === "user" ? "直接授权" : `部门：${g.subject.name}${g.include_sub ? "（含下级）" : ""}`}
                                  </span>
                                </div>
                              ))
                            ) : (
                              <Empty description="没有任何授权" />
                            )}
                          </Card>
                          <Card size="small" title="有效角色">
                            {eff.data.roles.length ? (
                              eff.data.roles.map((r) => (
                                <div key={r.id} style={{ display: "flex", justifyContent: "space-between", padding: "4px 0" }}>
                                  <Tag color="purple">
                                    {r.name} · {r.app}
                                  </Tag>
                                  <span className="uc-muted">{r.sources.join("、")}</span>
                                </div>
                              ))
                            ) : (
                              <span className="uc-muted">无</span>
                            )}
                          </Card>
                          <Card size="small" title="数据权限">
                            {eff.data.data.length ? (
                              eff.data.data.map((d) => (
                                <div key={d.id} style={{ display: "flex", justifyContent: "space-between", padding: "4px 0" }}>
                                  <span>
                                    <Tag>{d.data_code}</Tag>
                                    {d.data_name}
                                  </span>
                                  <LevelTag level={d.level} />
                                </div>
                              ))
                            ) : (
                              <span className="uc-muted">没有数据权限</span>
                            )}
                          </Card>
                        </Space>
                        <Card size="small" title="各应用的有效操作与可见菜单">
                          {eff.data.apps.map((a) => (
                            <div key={a.app.id} style={{ marginBottom: 14 }}>
                              <div style={{ fontWeight: 650 }}>
                                {a.app.icon} {a.app.name}
                              </div>
                              <div className="uc-muted" style={{ fontSize: 12.5, margin: "4px 0" }}>
                                菜单：{menuNames(a.menus).join("、") || "无"}
                              </div>
                              <div>
                                {a.permissions.length ? a.permissions.map((p) => <Tag key={p} className="uc-mono">{p}</Tag>) : <span className="uc-muted">无操作权限</span>}
                              </div>
                            </div>
                          ))}
                        </Card>
                      </div>
                    ) : (
                      <Card loading />
                    ),
                  },
                ]
              : []),
            {
              key: "logs",
              label: `登录记录（${logs.data?.length ?? 0}）`,
              children: (
                <Table
                  size="small"
                  rowKey={(r) => r.time}
                  dataSource={logs.data ?? []}
                  pagination={false}
                  columns={[
                    { title: "时间", render: (_, r) => fmtTime(r.time) },
                    { title: "IP", dataIndex: "ip", render: (v) => <span className="uc-mono">{v}</span> },
                    { title: "结果", render: (_, r) => (r.ok ? <Tag color="green">成功</Tag> : <Tag color="red">失败 · {r.reason}</Tag>) },
                  ]}
                />
              ),
            },
            {
              key: "apps",
              label: "可访问的应用",
              children: (apps.data ?? []).length ? (
                (apps.data ?? []).map((a) => (
                  <div key={a.id} style={{ padding: "6px 0" }}>
                    {a.icon} <b>{a.name}</b> <span className="uc-muted">{a.description}</span> {a.status === "disabled" && <Tag>应用已停用</Tag>}
                  </div>
                ))
              ) : (
                <Empty description="没有可访问的应用" />
              ),
            },
          ]}
        />
      </Card>
      <UserForm open={edit} user={u} onClose={() => setEdit(false)} />
    </>
  );
}
