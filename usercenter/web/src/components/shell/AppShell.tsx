"use client";

/**
 * 管理台骨架：顶部一级菜单 + 左侧二级菜单（可收缩）+ 内容区 —— 与 Atlas 的 AppShell 同一个样子。
 *
 * ★ 右上角「应用切换」：我能访问的应用（/me/apps，点开去它的入口地址）+ 用户中心自己；
 *   Atlas、TeamFlow 的顶栏放同一个入口，几个系统之间来回不用改地址栏。
 * ★ 收起状态记在本机（localStorage）。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Dropdown, Popover } from "antd";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { AppIcon } from "@/components/common";
import { Icon } from "@/components/Icon";
import { api } from "@/lib/api";
import type { AppInfo, Me } from "@/lib/types";

import { accessOf, itemOf, platformHome, visibleItems, visiblePlatforms } from "./nav";
import styles from "./shell.module.css";

const MIN_KEY = "uc.sider.min";

export function AppShell({ me, children }: { me: Me; children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const qc = useQueryClient();
  const access = accessOf(me);
  const [min, setMin] = useState(false);
  useEffect(() => setMin(localStorage.getItem(MIN_KEY) === "1"), []);

  const platforms = visiblePlatforms(access);
  const current = itemOf(pathname)?.platform ?? platforms[0]!;
  const items = visibleItems(current, access);

  function toggle() {
    setMin((v) => {
      localStorage.setItem(MIN_KEY, v ? "0" : "1");
      return !v;
    });
  }

  async function logout() {
    await api.post("/auth/logout");
    qc.clear();
    router.replace("/login");
  }

  return (
    <div className={styles.app}>
      <header className={styles.top}>
        <Link href={platformHome(platforms[0]!, access)} className={styles.brand}>
          <span className={styles.logo}>A</span>用户中心
        </Link>
        <nav className={styles.platforms} aria-label="一级菜单">
          {platforms.map((p) => (
            <Link key={p.id} href={platformHome(p, access)} className={styles.platform} aria-current={p.id === current.id ? "page" : undefined}>
              <Icon name={p.icon} />
              {p.label}
            </Link>
          ))}
        </nav>
        <div className={styles.topRight}>
          <AppSwitcher />
          <Dropdown
            trigger={["click"]}
            placement="bottomRight"
            menu={{
              items: [
                {
                  key: "who",
                  disabled: true,
                  label: (
                    <div style={{ color: "var(--fg)", minWidth: 200 }}>
                      <div style={{ fontWeight: 600 }}>{me.user.name}</div>
                      <div style={{ fontSize: 12, color: "var(--fg-3)" }}>
                        {[me.user.account, me.user.dept_path.at(-1), me.tenant.name].filter(Boolean).join(" · ")}
                      </div>
                    </div>
                  ),
                },
                { type: "divider" },
                { key: "/me", icon: <Icon name="user" size={14} />, label: "个人中心" },
                { key: "/me/security", icon: <Icon name="lock" size={14} />, label: "账号安全" },
                { type: "divider" },
                { key: "logout", icon: <Icon name="logout" size={14} />, label: "退出登录", danger: true },
              ],
              onClick: ({ key }) => (key === "logout" ? void logout() : router.push(key)),
            }}
          >
            <button type="button" className={styles.me} title={me.user.name} aria-label="我的账号">
              {me.user.name.slice(-1)}
            </button>
          </Dropdown>
        </div>
      </header>

      <div className={styles.body}>
        <aside className={`${styles.sider} ${min ? styles.siderMin : ""}`} aria-label={`${current.label}菜单`}>
          <div className={styles.siderTop}>
            {items.map((it) => (
              <Link key={it.href} href={it.href} className={styles.item} aria-current={it.match(pathname) ? "page" : undefined} title={it.label}>
                <Icon name={it.icon} />
                <span className={styles.label}>{it.label}</span>
              </Link>
            ))}
          </div>
          {current.id === "apps" && !min ? <AppList pathname={pathname} /> : <div className={styles.spacer} />}
          <div className={styles.siderFoot}>
            <button type="button" className={styles.item} onClick={toggle} title={min ? "展开菜单" : "收起菜单"}>
              <Icon name={min ? "right" : "left"} />
              <span className={styles.label}>收起</span>
            </button>
          </div>
        </aside>
        <main className={styles.main}>{children}</main>
      </div>
    </div>
  );
}

/** 应用平台的左侧：直接列出全部应用，配几个应用时不用来回返回目录 */
function AppList({ pathname }: { pathname: string }) {
  const { data = [] } = useQuery({ queryKey: ["apps"], queryFn: () => api.get<AppInfo[]>("/apps") });
  if (!data.length) return <div className={styles.spacer} />;
  return (
    <div className={styles.list}>
      <div className={styles.listHead}>已接入的应用</div>
      {data.map((a) => (
        <Link key={a.id} href={`/apps/${a.id}`} className={styles.listItem} aria-current={pathname === `/apps/${a.id}` ? "page" : undefined}>
          <AppIcon app={a} size={22} />
          <span className={styles.listName}>{a.name}</span>
          {a.is_builtin ? <span className={styles.tag}>内置</span> : a.status === "disabled" && <span className={styles.tag}>停用</span>}
        </Link>
      ))}
    </div>
  );
}

/** 应用切换：我能访问、且登记了入口地址的应用；用户中心自己放第一个 */
function AppSwitcher() {
  const [open, setOpen] = useState(false);
  const { data = [] } = useQuery({ queryKey: ["me", "apps"], queryFn: () => api.get<AppInfo[]>("/me/apps"), enabled: open, staleTime: 60_000 });
  const apps = data.filter((a) => a.home_url);
  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger="click"
      placement="bottomRight"
      arrow={false}
      content={
        <div className={styles.switcher}>
          <div className={styles.switcherHead}>切换应用</div>
          <div className={styles.apps}>
            <span className={styles.appTile} aria-current="page">
              <AppIcon app={{ icon: null, name: "用" }} size={36} />
              用户中心
            </span>
            {apps.map((a) => (
              // 入口地址是网关上的其他前端（或外部地址），整页跳转，不走 Next 路由
              <a key={a.id} href={a.home_url!} className={styles.appTile}>
                <AppIcon app={a} size={36} />
                {a.name}
              </a>
            ))}
          </div>
          <div className={styles.switcherFoot}>
            <Link href="/me/apps" onClick={() => setOpen(false)}>
              查看我的全部应用
            </Link>
          </div>
        </div>
      }
    >
      <button type="button" className={styles.iconBtn} title="切换应用" aria-label="切换应用">
        <Icon name="waffle" size={18} />
      </button>
    </Popover>
  );
}
