"use client";

/**
 * 控制台骨架：顶部一级菜单（平台）+ 左侧二级菜单（可收缩）+ 内容区。
 *
 * ★ 智能平台的左侧额外放「新对话」和「最近对话」（同 ChatGPT / Claude）。
 * ★ 收起状态记在本机（localStorage）；编辑智能体时默认收起，在那里展开只对编辑页生效。
 */
import { useQueryClient } from "@tanstack/react-query";
import { Dropdown } from "antd";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { useGwMe } from "@/api/mcp-gateway";
import { useMgMe } from "@/api/model-gateway";
import { useSkMe } from "@/api/skill-app";
import { Icon } from "@/components/Icon";
import { ThreadHistory } from "@/features/chat/ThreadHistory";
import { authHeaders } from "@/lib/http";
import { useMe } from "@/lib/session";

import { isEditorPath, PLATFORMS, platformHome, platformOf, visibleItems, type Access } from "./nav";
import styles from "./shell.module.css";

const MIN_KEY = "atlas.sider.min";

/** 没有骨架的整页：文件预览在新标签里打开，登录页只做跳转 */
const BARE = (p: string) => p.startsWith("/files/") || p === "/login";

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const qc = useQueryClient();
  const { data: me } = useMe();
  const { data: sk } = useSkMe();
  const { data: gw } = useGwMe();
  const { data: mg } = useMgMe();
  const access: Access = { me, sk, gw, mg };

  const [collapsed, setCollapsed] = useState(false);
  const [editorExpand, setEditorExpand] = useState(false);
  useEffect(() => setCollapsed(localStorage.getItem(MIN_KEY) === "1"), []);
  const inEditor = isEditorPath(pathname);
  useEffect(() => setEditorExpand(false), [pathname]);
  const min = inEditor ? !editorExpand : collapsed;

  if (BARE(pathname)) return <>{children}</>;

  const current = platformOf(pathname);
  const platforms = PLATFORMS.filter((p) => p.visible(access));
  const platform = PLATFORMS.find((p) => p.id === current)!;
  const items = visibleItems(platform, access);

  const toggle = () => {
    if (inEditor) return setEditorExpand((v) => !v);
    setCollapsed((v) => {
      localStorage.setItem(MIN_KEY, v ? "0" : "1");
      return !v;
    });
  };

  /** 登出在用户中心（几个前端共用一个登录态）：登出后去用户中心登录页 */
  async function logout() {
    await fetch("/uc/api/v1/auth/logout", { method: "POST", headers: authHeaders() });
    qc.clear();
    window.location.assign("/uc/login");
  }

  return (
    <div className={styles.app}>
      <header className={styles.top}>
        <Link href="/" className={styles.brand}>
          <span className={styles.logo}>A</span>Atlas
        </Link>
        <nav className={styles.platforms} aria-label="平台">
          {platforms.map((p) => (
            <Link
              key={p.id}
              href={platformHome(p, access)}
              className={styles.platform}
              aria-current={p.id === current ? "page" : undefined}
            >
              <Icon name={p.icon} size={16} />
              {p.label}
            </Link>
          ))}
        </nav>
        <div className={styles.topRight}>
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
                      <div style={{ fontSize: 12, color: "var(--fg-3)" }}>
                        {[me?.account, me?.dept].filter(Boolean).join(" · ")}
                      </div>
                    </div>
                  ),
                },
                { type: "divider" },
                { key: "me", icon: <Icon name="user" size={14} />, label: "个性化" },
                { key: "logout", icon: <Icon name="logout" size={14} />, label: "退出登录" },
              ],
              onClick: ({ key }) => {
                if (key === "me") router.push("/me");
                if (key === "logout") void logout();
              },
            }}
          >
            <button type="button" className={styles.me} title={me?.name ?? "我"} aria-label="我的账号">
              {(me?.name ?? "我").slice(-1)}
            </button>
          </Dropdown>
        </div>
      </header>

      <div className={styles.body}>
        <aside className={`${styles.sider} ${min ? styles.siderMin : ""}`} aria-label={`${platform.label}菜单`}>
          <div className={styles.siderTop}>
            {current === "ai" && (
              <Link href="/" className={styles.newChat} title="新对话">
                <Icon name="plus" size={16} />
                <span className={styles.label}>新对话</span>
              </Link>
            )}
            {items.map((it) => (
              <Link
                key={it.href}
                href={it.href}
                className={styles.item}
                aria-current={it.match(pathname) ? "page" : undefined}
                title={it.label}
              >
                <Icon name={it.icon} size={16} />
                <span className={styles.label}>{it.label}</span>
              </Link>
            ))}
          </div>
          {current === "ai" && !min ? <ThreadHistory /> : <div className={styles.spacer} />}
          <div className={styles.siderFoot}>
            <button type="button" className={styles.item} onClick={toggle} title={min ? "展开菜单" : "收起菜单"}>
              <Icon name={min ? "right" : "left"} size={16} />
              <span className={styles.label}>收起</span>
            </button>
          </div>
        </aside>
        <main className={styles.main}>{children}</main>
      </div>
    </div>
  );
}
