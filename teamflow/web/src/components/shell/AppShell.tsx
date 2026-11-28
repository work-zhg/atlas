"use client";

/**
 * 骨架（和 Atlas 一样）：顶栏一级菜单（项目 / 团队 / 流程市场）+ 左侧二级菜单（可收起）+ 内容区。
 *
 * ★ 项目的二级菜单下面列当前团队的项目（像 Atlas 的最近对话），点一下切换。
 * ★ 系统租户的人只用流程市场（运营管理员维护市场模板）。
 * ★ 顶栏右侧：团队切换、通知、我的账号。
 */
import { useQueryClient } from "@tanstack/react-query";
import { BellOutlined } from "@ant-design/icons";
import { App, Badge, Dropdown, Input, Modal, Popover } from "antd";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { Icon, type IconName } from "@/components/ui";
import { api, ApiError, errorText, logoutToUC, ucLoginUrl } from "@/lib/api";
import { ago, LEVEL } from "@/lib/format";
import { useMe, useNotifications, useProcess, useProjects } from "@/lib/queries";
import { useCan, useCurrentTeam } from "@/lib/team";
import type { Project, Team } from "@/lib/types";

import styles from "./shell.module.css";

const MIN_KEY = "teamflow.sider.min";

type PlatformId = "projects" | "team" | "market";
const PLATFORMS: { id: PlatformId; label: string; icon: IconName; href: string }[] = [
  { id: "projects", label: "项目", icon: "layers", href: "/projects" },
  { id: "team", label: "团队", icon: "users", href: "/team" },
  { id: "market", label: "流程市场", icon: "grid", href: "/market" },
];

export const MARKET_CATEGORIES: { key: string; label: string; icon: IconName }[] = [
  { key: "all", label: "全部", icon: "grid" },
  { key: "rd", label: "软件研发", icon: "terminal" },
  { key: "content", label: "内容", icon: "pencil" },
  { key: "legal", label: "法务与合规", icon: "shield" },
  { key: "data", label: "数据分析", icon: "layers" },
];

function platformOf(path: string): PlatformId | null {
  if (path.startsWith("/projects") || path.startsWith("/flows")) return "projects";
  if (path.startsWith("/team")) return "team";
  if (path.startsWith("/market")) return "market";
  return null;
}

/** 整页编辑：节点页、编排器默认收起左侧 */
const isWide = (p: string) => /^\/flows\/[^/]+\/n\//.test(p) || /^\/team\/templates\/[^/]+/.test(p) || /^\/market\/[^/]+\/edit/.test(p);

interface Item {
  href: string;
  label: string;
  icon: IconName;
  on: boolean;
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { data: me, error } = useMe();

  useEffect(() => {
    if (error instanceof ApiError && error.status === 401) window.location.assign(ucLoginUrl());
  }, [error]);
  // 系统租户的人只用流程市场（个人设置、定时任务除外）
  const opsOnly = !!me?.system_tenant;
  const outOfOps = opsOnly && !/^\/(market|me|jobs)/.test(pathname);
  useEffect(() => {
    if (outOfOps) router.replace("/market");
  }, [outOfOps, router]);

  const [collapsed, setCollapsed] = useState(false);
  const [wideOpen, setWideOpen] = useState(false);
  useEffect(() => setCollapsed(localStorage.getItem(MIN_KEY) === "1"), []);
  useEffect(() => setWideOpen(false), [pathname]);
  const wide = isWide(pathname);
  const min = wide ? !wideOpen : collapsed;
  const toggle = () => {
    if (wide) return setWideOpen((v) => !v);
    setCollapsed((v) => {
      localStorage.setItem(MIN_KEY, v ? "0" : "1");
      return !v;
    });
  };

  const current = platformOf(pathname);
  const platforms = PLATFORMS.filter((p) => !opsOnly || p.id === "market");

  return (
    <div className={styles.app}>
      <header className={styles.top}>
        <Link href={opsOnly ? "/market" : "/projects"} className={styles.brand}>
          <span className={styles.logo}>T</span>AI TeamFlow
        </Link>
        {!opsOnly && <TeamSwitch />}
        <nav className={styles.platforms} aria-label="平台">
          {platforms.map((p) => (
            <Link key={p.id} href={p.href} className={styles.platform} aria-current={p.id === current ? "page" : undefined}>
              <Icon name={p.icon} size={16} />
              {p.label}
            </Link>
          ))}
        </nav>
        <div className={styles.topRight}>
          {!opsOnly && <Bell />}
          <MeMenu systemTenant={opsOnly} />
        </div>
      </header>
      <div className={styles.body}>
        <Sider platform={current} min={min} onToggle={toggle} />
        <main className={styles.main}>{children}</main>
      </div>
    </div>
  );
}

function Sider({ platform, min, onToggle }: { platform: PlatformId | null; min: boolean; onToggle: () => void }) {
  const pathname = usePathname();
  const can = useCan();
  const flowId = pathname.match(/^\/flows\/([^/]+)/)?.[1];
  const flow = useProcess(flowId);
  const projectId = pathname.match(/^\/projects\/([^/]+)/)?.[1] ?? flow.data?.project_id;
  const cat = useSearchParams().get("cat");

  let items: Item[] = [];
  if (platform === "projects" && projectId) {
    const cfg = pathname === `/projects/${projectId}/config`;
    items = [
      { href: `/projects/${projectId}`, label: "流程", icon: "share", on: !cfg },
      { href: `/projects/${projectId}/config`, label: "配置", icon: "sliders", on: cfg },
    ];
  } else if (platform === "team") {
    items = [
      { href: "/team", label: "成员与数字员工", icon: "users", on: pathname === "/team" },
      ...(can.configFlow
        ? [{ href: "/team/templates", label: "流程模板", icon: "list" as IconName, on: pathname.startsWith("/team/templates") }]
        : []),
      ...(can.configTeam ? [{ href: "/team/settings", label: "团队设置", icon: "cog" as IconName, on: pathname === "/team/settings" }] : []),
    ];
  } else if (platform === "market") {
    const onList = pathname === "/market";
    items = MARKET_CATEGORIES.map((c) => ({
      href: c.key === "all" ? "/market" : `/market?cat=${c.key}`,
      label: c.label,
      icon: c.icon,
      on: onList && (cat ?? "all") === c.key,
    }));
  }

  return (
    <aside className={`${styles.sider} ${min ? styles.siderMin : ""}`}>
      <div className={styles.siderTop}>
        {items.map((it) => (
          <Link key={it.href} href={it.href} className={styles.item} aria-current={it.on ? "page" : undefined} title={it.label}>
            <Icon name={it.icon} size={16} />
            <span className={styles.label}>{it.label}</span>
          </Link>
        ))}
      </div>
      {platform === "projects" && !min ? <ProjectList current={projectId} /> : <div className={styles.spacer} />}
      <div className={styles.siderFoot}>
        <button type="button" className={styles.item} onClick={onToggle} title={min ? "展开菜单" : "收起菜单"}>
          <Icon name={min ? "right" : "left"} size={16} />
          <span className={styles.label}>收起</span>
        </button>
      </div>
    </aside>
  );
}

/** 本团队的项目：点一下切换（停在「配置」页时切过去也是配置页） */
function ProjectList({ current }: { current: string | undefined }) {
  const pathname = usePathname();
  const { team } = useCurrentTeam();
  const can = useCan();
  const { data: projects } = useProjects(team?.id);
  const [creating, setCreating] = useState(false);
  const tail = pathname.endsWith("/config") ? "/config" : "";
  const list = (projects ?? []).filter((p) => p.status === "active");
  return (
    <div className={styles.hist}>
      <div className={styles.histHead}>
        <span>项目 · {team?.name ?? "—"}</span>
        {can.configFlow && (
          <button type="button" className={styles.iconBtn} title="新建项目" onClick={() => setCreating(true)}>
            <Icon name="plus" size={14} />
          </button>
        )}
      </div>
      {list.map((p) => (
        <Link
          key={p.id}
          href={`/projects/${p.id}${tail}`}
          className={styles.histItem}
          aria-current={p.id === current ? "page" : undefined}
          title={p.description ?? p.name}
        >
          <Icon name="layers" size={14} />
          <span className={styles.histTitle}>{p.name}</span>
          {!!p.todos && <span className={styles.badge} title="等你处理">{p.todos}</span>}
        </Link>
      ))}
      {projects && !list.length && (
        <div className="xs faint" style={{ padding: "8px 10px" }}>
          还没有项目{can.configFlow ? "" : "，等团队管理员建"}
        </div>
      )}
      {creating && team && <NewProjectModal team={team} onClose={() => setCreating(false)} />}
    </div>
  );
}

function TeamSwitch() {
  const { teams, team, setTeam } = useCurrentTeam();
  const can = useCan();
  const router = useRouter();
  const [creating, setCreating] = useState(false);
  return (
    <>
      <Dropdown
        trigger={["click"]}
        menu={{
          items: [
            ...teams.map((t) => ({
              key: t.id,
              label: (
                <div className="row" style={{ minWidth: 220 }}>
                  <span className="grow">{t.name}</span>
                  <span className="xs faint">{LEVEL[t.my_level ?? "NONE"].label}</span>
                </div>
              ),
            })),
            ...(can.createTeam ? [{ type: "divider" as const }, { key: "__new", icon: <Icon name="plus" size={14} />, label: "新建团队" }] : []),
          ],
          selectedKeys: team ? [team.id] : [],
          onClick: ({ key }) => {
            if (key === "__new") return setCreating(true);
            setTeam(key);
            router.push("/projects");
          },
        }}
      >
        <button type="button" className="row" style={{ border: "1px solid var(--line)", background: "var(--bg-1)", borderRadius: 8, height: 30, padding: "0 10px", cursor: "pointer", fontWeight: 550 }}>
          <Icon name="users" size={14} />
          {team?.name ?? (teams.length ? "选择团队" : "还没有团队")}
          <Icon name="down" size={14} />
        </button>
      </Dropdown>
      {creating && <NewTeamModal onClose={() => setCreating(false)} onCreated={(t) => setTeam(t.id)} />}
    </>
  );
}

function Bell() {
  const { data } = useNotifications();
  const qc = useQueryClient();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const readAll = async () => {
    await api.post("/notifications/read", {});
    await qc.invalidateQueries({ queryKey: ["notifications"] });
  };
  const items = data?.items ?? [];
  const body = (
    <div style={{ width: 360, maxHeight: 440, overflowY: "auto" }}>
      <div className="row" style={{ padding: "2px 4px 8px" }}>
        <b>通知</b>
        <span className="sp" />
        {!!data?.unread && (
          <a className="xs" onClick={() => void readAll()}>
            全部已读
          </a>
        )}
      </div>
      {items.length ? (
        items.map((n) => (
          <div
            key={n.id}
            className="row"
            style={{ alignItems: "flex-start", padding: "8px 4px", borderTop: "1px solid var(--tf-line-soft)", cursor: n.link ? "pointer" : undefined }}
            onClick={() => {
              if (!n.read) void api.post("/notifications/read", { ids: [n.id] }).then(() => qc.invalidateQueries({ queryKey: ["notifications"] }));
              if (n.link) {
                setOpen(false);
                router.push(n.link);
              }
            }}
          >
            <span style={{ width: 7, height: 7, borderRadius: "50%", marginTop: 7, flex: "none", background: n.read ? "transparent" : "var(--warning)" }} />
            <div className="grow">
              <div className="sm">{n.text}</div>
              <div className="xs faint">{ago(n.at)}</div>
            </div>
          </div>
        ))
      ) : (
        <div className="sm muted" style={{ padding: "16px 4px" }}>
          没有通知
        </div>
      )}
      <div className="xs faint" style={{ padding: "8px 4px 0", borderTop: "1px solid var(--tf-line-soft)" }}>
        也能推到企业微信 / 钉钉：在 <Link href="/me">个人设置</Link> 里绑定
      </div>
    </div>
  );
  return (
    <Popover content={body} trigger="click" placement="bottomRight" open={open} onOpenChange={setOpen}>
      <button type="button" className={styles.iconBtn} style={{ width: 32, height: 32 }} title="通知：要你评审的事">
        <Badge count={data?.unread ?? 0} size="small" offset={[2, -2]}>
          <BellOutlined style={{ fontSize: 17 }} />
        </Badge>
      </button>
    </Popover>
  );
}

function MeMenu({ systemTenant }: { systemTenant: boolean }) {
  const { data: me } = useMe();
  const qc = useQueryClient();
  const router = useRouter();
  return (
    <Dropdown
      trigger={["click"]}
      placement="bottomRight"
      menu={{
        items: [
          {
            key: "who",
            disabled: true,
            label: (
              <div style={{ color: "var(--fg)", minWidth: 180 }}>
                <div style={{ fontWeight: 600 }}>{me?.name ?? "—"}</div>
                {systemTenant && <div style={{ fontSize: 12, color: "var(--fg-3)" }}>系统租户 · {me?.market === "maintain" ? "运营管理员" : "只读"}</div>}
              </div>
            ),
          },
          { type: "divider" },
          { key: "me", icon: <Icon name="user" size={14} />, label: "个人设置" },
          ...(systemTenant ? [{ key: "jobs", icon: <Icon name="clock" size={14} />, label: "定时任务" }] : []),
          { key: "logout", icon: <Icon name="logout" size={14} />, label: "退出登录" },
        ],
        onClick: ({ key }) => {
          if (key === "me") router.push("/me");
          if (key === "jobs") router.push("/jobs");
          if (key === "logout") {
            qc.clear();
            void logoutToUC();
          }
        },
      }}
    >
      <button type="button" className={styles.me} title={me?.name ?? "我"} aria-label="我的账号">
        {(me?.name ?? "我").slice(0, 1)}
      </button>
    </Dropdown>
  );
}

export function NewTeamModal({ onClose, onCreated }: { onClose: () => void; onCreated: (t: Team) => void }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const router = useRouter();
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [busy, setBusy] = useState(false);
  const ok = async () => {
    setBusy(true);
    try {
      const t = await api.post<Team>("/teams", { name: name.trim(), description: desc.trim() || null });
      await qc.invalidateQueries({ queryKey: ["teams"] });
      onCreated(t);
      onClose();
      router.push("/team");
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open title="新建团队" onCancel={onClose} onOk={ok} okText="创建" confirmLoading={busy} okButtonProps={{ disabled: !name.trim() }}>
      <div className="col" style={{ gap: 12 }}>
        <div>
          <div className="sm" style={{ marginBottom: 6 }}>团队名称</div>
          <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={64} placeholder="如：研发二部" />
          <div className="xs faint" style={{ marginTop: 4 }}>在本租户里唯一</div>
        </div>
        <div>
          <div className="sm" style={{ marginBottom: 6 }}>说明（可选）</div>
          <Input value={desc} onChange={(e) => setDesc(e.target.value)} maxLength={200} placeholder="这个团队做什么" />
        </div>
        <div className="banner info">
          <Icon name="shield" size={15} />
          <div>新建团队的人自动成为它的团队管理员（用户中心的团队 Owner）；成员建好后在「团队 → 成员与数字员工」里加</div>
        </div>
      </div>
    </Modal>
  );
}

export function NewProjectModal({ team, onClose }: { team: Team; onClose: () => void }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const router = useRouter();
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [busy, setBusy] = useState(false);
  const ok = async () => {
    setBusy(true);
    try {
      const p = await api.post<Project>(`/teams/${team.id}/projects`, { name: name.trim(), description: desc.trim() || null });
      await qc.invalidateQueries({ queryKey: ["team", team.id, "projects"] });
      onClose();
      router.push(`/projects/${p.id}/config`);
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open title={`新建项目 · ${team.name}`} onCancel={onClose} onOk={ok} okText="创建" confirmLoading={busy} okButtonProps={{ disabled: !name.trim() }}>
      <div className="col" style={{ gap: 12 }}>
        <div>
          <div className="sm" style={{ marginBottom: 6 }}>项目名称</div>
          <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={64} />
          <div className="xs faint" style={{ marginTop: 4 }}>团队内唯一</div>
        </div>
        <div>
          <div className="sm" style={{ marginBottom: 6 }}>说明（可选）</div>
          <Input value={desc} onChange={(e) => setDesc(e.target.value)} maxLength={200} placeholder="这个项目做什么" />
        </div>
        <div className="xs muted">建好后到「配置」绑定流程模板、给角色分配成员和数字员工</div>
      </div>
    </Modal>
  );
}
