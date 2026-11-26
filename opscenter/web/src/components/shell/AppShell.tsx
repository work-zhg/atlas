"use client";

/**
 * 运营中心的骨架：顶部一级菜单 + 左侧二级菜单（可收缩）+ 内容区 —— 与 Atlas、用户中心同一个样子。
 *
 * ★ 运营人员是用户中心里系统租户的人（统一登录）：右上角放环境名和账号菜单（退出登录）；
 *   改密码、开账号、授模块都在用户中心。菜单按这个人能看的模块显示（nav.ts）。
 * ★ 品牌旁的「平台」标签 + 环境名：浅色之后和 Atlas 长得像，靠这两处提醒「这里改的是整个平台」。
 * ★ 收起状态记在本机（localStorage）。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Dropdown } from "antd";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { AppIcon, TenantIcon } from "@/components/common";
import { Icon } from "@/components/Icon";
import { UC, api, logout as ucLogout, sys } from "@/lib/api";
import { TENANT_MODULE, access, useSystems } from "@/lib/session";
import type { AppInfo, Me, Tenant } from "@/lib/types";

import { isLink, platformHome, platformOf, visibleItems, visiblePlatforms, type NavCtx } from "./nav";
import styles from "./shell.module.css";

const MIN_KEY = "ops.sider.min";

export function AppShell({ me, children }: { me: Me; children: ReactNode }) {
  const pathname = usePathname();
  const qc = useQueryClient();
  const { data: systems = [] } = useSystems();
  const [min, setMin] = useState(false);
  const [host, setHost] = useState("");
  useEffect(() => {
    setMin(localStorage.getItem(MIN_KEY) === "1");
    setHost(window.location.host);
  }, []);

  const current = platformOf(pathname);
  const ctx: NavCtx = { access: access(me, systems), systems };
  const platforms = visiblePlatforms(ctx);
  const items = visibleItems(current, ctx);
  const granted = me.modules.filter((m) => m.level !== "NONE").length;

  function toggle() {
    setMin((v) => {
      localStorage.setItem(MIN_KEY, v ? "0" : "1");
      return !v;
    });
  }

  async function logout() {
    qc.clear();
    await ucLogout();
  }

  return (
    <div className={styles.app}>
      <header className={styles.top}>
        <Link href="/" className={styles.brand}>
          <span className={styles.logo}>A</span>运营中心
          <span className={styles.brandTag} title="管的是整个平台：所有租户、所有系统">
            平台
          </span>
        </Link>
        <nav className={styles.platforms} aria-label="一级菜单">
          {platforms.map((p) => (
            <Link key={p.id} href={platformHome(p, ctx)} className={styles.platform} aria-current={p.id === current.id ? "page" : undefined}>
              <Icon name={p.icon} />
              {p.label}
            </Link>
          ))}
        </nav>
        <div className={styles.topRight}>
          {host && (
            <span className={styles.env} title="当前连的是哪套环境">
              <Icon name="server" size={13} />
              {host}
            </span>
          )}
          <Dropdown
            trigger={["click"]}
            placement="bottomRight"
            menu={{
              items: [
                {
                  key: "who",
                  disabled: true,
                  label: (
                    <div style={{ color: "var(--fg)", minWidth: 220 }}>
                      <div style={{ fontWeight: 600 }}>{me.user.name}</div>
                      <div style={{ fontSize: 12, color: "var(--fg-3)" }}>
                        系统租户 · 能进 {granted} 个模块{me.can_audit ? " · 运营审计" : ""}
                      </div>
                      <div style={{ fontSize: 12, color: "var(--fg-3)" }}>改密码、要权限请到用户中心</div>
                    </div>
                  ),
                },
                { type: "divider" },
                { key: "logout", icon: <Icon name="logout" size={14} />, label: "退出登录", danger: true },
              ],
              onClick: ({ key }) => {
                if (key === "logout") void logout();
              },
            }}
          >
            <button type="button" className={styles.meChip} aria-label="我的账号">
              <span className={styles.avatar}>{me.user.name.slice(-1)}</span>
              {me.user.name}
              <span className={styles.role} data-admin={ctx.access.write(TENANT_MODULE)}>
                {granted} 个模块
              </span>
              <Icon name="down" size={14} />
            </button>
          </Dropdown>
        </div>
      </header>

      <div className={styles.body}>
        <aside className={`${styles.sider} ${min ? styles.siderMin : ""}`} aria-label={`${current.label}菜单`}>
          <div className={styles.siderTop}>
            {items.map((it) =>
              isLink(it) ? (
                <Link key={it.href} href={it.href} className={styles.item} aria-current={it.match(pathname) ? "page" : undefined} title={it.label}>
                  <Icon name={it.icon} />
                  <span className={styles.label}>{it.label}</span>
                </Link>
              ) : (
                <div key={it.group} className={styles.group}>
                  {it.group}
                </div>
              ),
            )}
          </div>
          {!min && current.id === "tenants" ? (
            <RunningList pathname={pathname} />
          ) : !min && current.id === "apps" ? (
            <AppList pathname={pathname} />
          ) : (
            <div className={styles.spacer} />
          )}
          <div className={styles.siderFoot}>
            <button type="button" className={styles.item} onClick={toggle} title={min ? "展开菜单" : "收起菜单"}>
              <Icon name={min ? "right" : "left"} />
              <span className={styles.label}>收起</span>
            </button>
          </div>
        </aside>
        <main className={styles.main}>
          <div className="ops-page">{children}</div>
        </main>
      </div>
    </div>
  );
}

/** 租户平台的左侧：进行中的操作（开通、停用、恢复、关闭），卡住的标红，一点直达 */
function RunningList({ pathname }: { pathname: string }) {
  const { data = [] } = useQuery({ queryKey: ["tenants"], queryFn: () => api.get<Tenant[]>("/tenants"), refetchInterval: 10_000 });
  const running = data.filter((t) => t.operation);
  if (!running.length) return <div className={styles.spacer} />;
  return (
    <div className={styles.list}>
      <div className={styles.listHead}>进行中的操作</div>
      {running.map((t) => (
        <Link key={t.id} href={`/tenants/${t.id}`} className={styles.listItem} aria-current={pathname === `/tenants/${t.id}` ? "page" : undefined}>
          <TenantIcon tenant={t} size={22} />
          <span className={styles.listName}>{t.name}</span>
          <span className={styles.listTag} data-tone={t.operation!.stuck ? "danger" : "primary"}>
            {t.operation!.stuck ? "失败重试中" : "进行中"}
          </span>
        </Link>
      ))}
    </div>
  );
}

/** 应用平台的左侧：直接列出全部应用，看几个应用时不用来回返回目录 */
function AppList({ pathname }: { pathname: string }) {
  const { data = [] } = useQuery({ queryKey: ["apps"], queryFn: () => sys(UC).get<AppInfo[]>("/apps") });
  if (!data.length) return <div className={styles.spacer} />;
  return (
    <div className={styles.list}>
      <div className={styles.listHead}>已接入的应用</div>
      {data.map((a) => (
        <Link key={a.id} href={`/apps/${a.id}`} className={styles.listItem} aria-current={pathname === `/apps/${a.id}` ? "page" : undefined}>
          <AppIcon app={a} size={22} />
          <span className={styles.listName}>{a.name}</span>
          {a.status === "disabled" && (
            <span className={styles.listTag} data-tone="plain">
              停用
            </span>
          )}
        </Link>
      ))}
    </div>
  );
}
