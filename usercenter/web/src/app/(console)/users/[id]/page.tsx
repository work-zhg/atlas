"use client";

/**
 * 人员 / 数字员工详情（/digital/[id] 复用本页）。
 * 「权限」按应用分组列出有效角色，每个角色写清来源（直接授权、部门继承、岗位带来的）——
 * 直接回答「他为什么能 / 不能用」。
 */
import { useQuery } from "@tanstack/react-query";
import { Button, Card, Descriptions, Dropdown, Empty, Space, Table, Tabs, Tag } from "antd";
import type { MenuProps } from "antd";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { AppIcon, DetailHead, LevelTag, PersonAvatar, SquareIcon, StatusTag } from "@/components/common";
import { Icon } from "@/components/Icon";
import { UserForm } from "@/components/UserForm";
import { useUserActions } from "@/components/userActions";
import { api } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useCan, useMe } from "@/lib/session";
import type { AppInfo, GrantRow, Level, LoginLog, MenuNode, User } from "@/lib/types";

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
  const { data: me } = useMe();
  const actions = useUserActions();
  const [edit, setEdit] = useState(false);
  const user = useQuery({ queryKey: ["user", id], queryFn: () => api.get<User>(`/users/${id}`) });
  const digital = user.data?.user_type === "digital";
  const logs = useQuery({
    queryKey: ["user", id, "logs"],
    queryFn: () => api.get<LoginLog[]>(`/users/${id}/login-logs`),
    enabled: user.data !== undefined && !digital,
  });
  const apps = useQuery({ queryKey: ["user", id, "apps"], queryFn: () => api.get<AppInfo[]>(`/users/${id}/apps`) });
  const eff = useQuery({
    queryKey: ["user", id, "effective"],
    queryFn: () => api.get<Effective>(`/users/${id}/effective`),
    enabled: can("grant:view"),
  });
  const u = user.data;
  if (!u) return <Card loading />;

  const more: MenuProps["items"] = [
    can("user:disable") && u.status === "locked" && { key: "unlock", icon: <Icon name="unlock" size={14} />, label: "解锁", onClick: () => void actions.unlock(u) },
    can("user:disable") &&
      (u.status === "disabled"
        ? { key: "enable", icon: <Icon name="check" size={14} />, label: "启用", onClick: () => void actions.enable(u) }
        : { key: "disable", icon: <Icon name="ban" size={14} />, label: u.id === me?.user.id ? "停用（不能停用自己）" : "停用", danger: true, disabled: u.id === me?.user.id, onClick: () => actions.disable(u) }),
  ].filter(Boolean) as MenuProps["items"];

  // 有效角色按应用分组（后端给的是应用名）
  const byApp = new Map<string, Effective["roles"]>();
  for (const r of eff.data?.roles ?? []) byApp.set(r.app, [...(byApp.get(r.app) ?? []), r]);
  const grants = eff.data ? [...eff.data.grants.own, ...eff.data.grants.inherited] : [];

  const info = digital ? (
    <Descriptions column={1} size="small" styles={{ label: { width: 110 } }}>
      <Descriptions.Item label="负责人">
        {u.owner ? (
          <Space>
            <PersonAvatar name={u.owner.name} size={22} />
            <Link href={`/users/${u.owner.id}`}>{u.owner.name}</Link>
            {u.owner.status === "disabled" && <Tag color="warning">负责人已停用，请在来源应用里转交</Tag>}
          </Space>
        ) : (
          "—"
        )}
      </Descriptions.Item>
      <Descriptions.Item label="来源应用">{u.source_app?.name ?? "—"}</Descriptions.Item>
      <Descriptions.Item label="外部引用">
        <span className="uc-mono uc-muted">{u.external_ref ?? "—"}</span>
      </Descriptions.Item>
      {u.description && <Descriptions.Item label="说明">{u.description}</Descriptions.Item>}
      <Descriptions.Item label="岗位">
        {u.positions?.join("、") || "—"}
        {can("grant:view") && <Link href={`/perm/grants?subject=user:${u.id}`} style={{ marginLeft: 8 }}>去授权</Link>}
      </Descriptions.Item>
      {u.disabled_reason && <Descriptions.Item label="停用原因">{u.disabled_reason}</Descriptions.Item>}
      <Descriptions.Item label="能否登录">
        <span className="uc-muted">不能（只作为智能体运行时的身份）</span>
      </Descriptions.Item>
      <Descriptions.Item label="创建时间">{fmtTime(u.created_at)}</Descriptions.Item>
    </Descriptions>
  ) : (
    <Descriptions column={1} size="small" styles={{ label: { width: 110 } }}>
      <Descriptions.Item label="邮箱">{u.email}</Descriptions.Item>
      <Descriptions.Item label="手机">{u.phone || "—"}</Descriptions.Item>
      <Descriptions.Item label="部门">{u.dept_path?.join(" / ")}</Descriptions.Item>
      <Descriptions.Item label="岗位">
        {u.positions?.join("、") || "—"}
        {can("grant:view") && <Link href={`/perm/grants?subject=user:${u.id}`} style={{ marginLeft: 8 }}>去授权</Link>}
      </Descriptions.Item>
      <Descriptions.Item label="直属上级">
        {u.manager ? (
          <Space>
            <PersonAvatar name={u.manager.name} size={22} />
            <Link href={`/users/${u.manager.id}`}>{u.manager.name}</Link>
            <span className="uc-muted">（{u.manager.source === "own_dept" ? "所在部门负责人" : "上级部门负责人"}，按组织架构计算）</span>
          </Space>
        ) : (
          "—"
        )}
      </Descriptions.Item>
      {u.disabled_reason && <Descriptions.Item label="停用原因">{u.disabled_reason}</Descriptions.Item>}
      <Descriptions.Item label="创建时间">{fmtTime(u.created_at)}</Descriptions.Item>
      <Descriptions.Item label="最近登录">{u.last_login_at ? fmtTime(u.last_login_at) : "从未登录"}</Descriptions.Item>
    </Descriptions>
  );

  const perm = eff.data ? (
    <Space direction="vertical" size={14} style={{ width: "100%" }}>
      <Card
        size="small"
        title="各应用的有效角色"
        extra={<Link href={`/perm/grants?subject=user:${u.id}`}>在「权限 › 授权」中授权 →</Link>}
        styles={{ body: { padding: 0 } }}
      >
        {byApp.size ? (
          <Table
            size="small"
            rowKey={(r) => `${r.app}:${r.id}`}
            pagination={false}
            dataSource={[...byApp.values()].flat()}
            columns={[
              {
                title: "应用",
                width: 200,
                onCell: (r, i) => {
                  const rows = [...byApp.values()].flat();
                  const first = rows.findIndex((x) => x.app === r.app) === i;
                  return { rowSpan: first ? byApp.get(r.app)!.length : 0 };
                },
                render: (_, r) => <b>{r.app}</b>,
              },
              { title: "角色", render: (_, r) => <span className="uc-nw">{r.name}</span> },
              { title: "来源", render: (_, r) => <span className="uc-muted">{r.sources.join("；")}</span> },
            ]}
          />
        ) : (
          <Empty style={{ padding: 24 }} description={digital ? "没有任何角色：数字员工只认直接授给它的角色和岗位" : "没有任何角色"} />
        )}
      </Card>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, alignItems: "start" }}>
        <Card size="small" title={`生效的授权（${grants.length}）`}>
          {grants.length ? (
            grants.map((g) => (
              <div key={g.id} className="uc-row" style={{ justifyContent: "space-between", padding: "4px 0" }}>
                <span>
                  <Tag color={g.kind === "position" ? "blue" : "purple"}>{g.kind === "position" ? "岗位" : "角色"}</Tag>
                  {g.target.name}
                </span>
                <span className="uc-muted">{g.subject.type === "user" ? "直接授权" : `部门：${g.subject.name}${g.include_sub ? "（含下级）" : ""}`}</span>
              </div>
            ))
          ) : (
            <span className="uc-muted">没有任何授权</span>
          )}
        </Card>
        <Card size="small" title="数据权限">
          {eff.data.data.length ? (
            eff.data.data.map((d) => (
              <div key={d.id} className="uc-row" style={{ justifyContent: "space-between", padding: "4px 0" }}>
                <span>
                  <Tag className="uc-mono">{d.data_code}</Tag>
                  {d.data_name}
                </span>
                <LevelTag level={d.level} />
              </div>
            ))
          ) : (
            <span className="uc-muted">没有数据权限</span>
          )}
        </Card>
      </div>
      <Card size="small" title="各应用的有效操作与可见菜单">
        {eff.data.apps.map((a) => (
          <div key={a.app.id} style={{ marginBottom: 14 }}>
            <div className="uc-row" style={{ fontWeight: 650 }}>
              <AppIcon app={a.app} size={22} />
              {a.app.name}
            </div>
            <div className="uc-sub" style={{ margin: "4px 0" }}>
              菜单：{menuNames(a.menus).join("、") || "无"}
            </div>
            <div>{a.permissions.length ? a.permissions.map((p) => <Tag key={p} className="uc-mono">{p}</Tag>) : <span className="uc-muted">无操作权限</span>}</div>
          </div>
        ))}
      </Card>
    </Space>
  ) : (
    <Card loading />
  );

  return (
    <>
      <DetailHead
        back={digital ? "/digital" : "/users"}
        icon={digital ? <SquareIcon icon="bot" size={44} color="#0ea5e9" /> : <PersonAvatar name={u.name} size={44} />}
        title={u.name}
        tags={
          <>
            <StatusTag status={u.status} />
            {digital && <Tag color="cyan">数字员工</Tag>}
            {u.must_change_password && <Tag color="gold">须改密</Tag>}
            {u.uc_roles?.map((r) => (
              <Tag key={r} color="purple">
                {r}
              </Tag>
            ))}
          </>
        }
        sub={`${u.account} · ${digital ? `来源：${u.source_app?.name ?? "—"}` : u.dept_path?.join(" / ")}${u.positions?.length ? ` · ${u.positions.join("、")}` : ""}`}
        extra={
          <>
            {/* 数字员工的资料由来源应用维护，这里只能停用 / 启用（紧急止血） */}
            {can("user:edit") && !digital && (
              <Button icon={<Icon name="pencil" size={14} />} onClick={() => setEdit(true)}>
                编辑
              </Button>
            )}
            {can("user:reset_pwd") && !digital && u.status !== "disabled" && (
              <Button icon={<Icon name="key" size={14} />} onClick={() => actions.reset(u)}>
                重置密码
              </Button>
            )}
            {!!more?.length && (
              <Dropdown trigger={["click"]} menu={{ items: more }}>
                <Button type="text" aria-label="更多" icon={<Icon name="more" />} />
              </Dropdown>
            )}
          </>
        }
      />
      <Tabs
        items={[
          { key: "info", label: "基本信息", children: <Card>{info}</Card> },
          ...(can("grant:view") ? [{ key: "perm", label: "权限", children: perm }] : []),
          // 数字员工不能登录，没有登录记录
          ...(digital
            ? []
            : [
                {
                  key: "logs",
                  label: "登录记录",
                  children: (
                    <Card styles={{ body: { padding: 0 } }}>
                      <Table<LoginLog>
                        rowKey={(r) => r.time}
                        dataSource={logs.data ?? []}
                        pagination={false}
                        locale={{ emptyText: "最近 90 天没有登录记录" }}
                        columns={[
                          { title: "时间", render: (_, r) => fmtTime(r.time) },
                          { title: "IP", render: (_, r) => <span className="uc-mono">{r.ip ?? "—"}</span> },
                          { title: "经由", render: (_, r) => <span className="uc-muted">{r.app ?? "统一登录"}</span> },
                          { title: "结果", render: (_, r) => (r.ok ? <Tag color="success">成功</Tag> : <Tag color="error">失败 · {r.reason}</Tag>) },
                        ]}
                      />
                    </Card>
                  ),
                },
              ]),
          {
            key: "apps",
            label: "可访问的应用",
            children: (apps.data ?? []).length ? (
              <div className="uc-grid">
                {(apps.data ?? []).map((a) => (
                  <div key={a.id} className="uc-acard">
                    <div className="uc-row" style={{ gap: 10 }}>
                      <AppIcon app={a} />
                      <div className="uc-grow">
                        <b>{a.name}</b>
                        <div className="uc-sub">{a.status === "disabled" ? "应用已停用" : a.description}</div>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <Empty description="没有可访问的应用" />
            ),
          },
        ]}
      />
      <UserForm open={edit} user={u} onClose={() => setEdit(false)} />
    </>
  );
}
