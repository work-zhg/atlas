"use client";

/** 运营控制台的框架：左侧导航、当前运营人员、登出。没登录去登录页，临时密码先去改密码。 */
import { useQueryClient } from "@tanstack/react-query";
import { Avatar, Button, Spin, Tag } from "antd";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { ApiError, api } from "@/lib/api";
import { useMe } from "@/lib/session";

const NAV: { group?: string; items: { path: string; name: string; icon: string }[] }[] = [
  {
    items: [
      { path: "/tenants", name: "租户", icon: "🏢" },
      { path: "/apps", name: "应用目录", icon: "🧩" },
      { path: "/capacity", name: "容量", icon: "📊" },
    ],
  },
  {
    group: "系统设置",
    items: [
      { path: "/settings/templates", name: "沙箱模板", icon: "📦" },
      { path: "/settings/params", name: "系统参数", icon: "⚙️" },
      { path: "/settings/jobs", name: "系统任务", icon: "⏱" },
      { path: "/settings/skills", name: "技能市场", icon: "🛒" },
      { path: "/settings/scopes", name: "记忆作用域", icon: "🧠" },
    ],
  },
  {
    group: "运营",
    items: [
      { path: "/operators", name: "运营账号", icon: "👤" },
      { path: "/audit", name: "运营审计", icon: "📜" },
    ],
  },
];

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const qc = useQueryClient();
  const { data: me, error } = useMe();

  useEffect(() => {
    if (error instanceof ApiError && error.status === 401) router.replace("/login");
    else if (error instanceof ApiError && error.code === "PASSWORD_CHANGE_REQUIRED") router.replace("/password");
    else if (me?.must_change_password) router.replace("/password");
  }, [me, error, router]);

  if (!me || me.must_change_password) return <Spin style={{ display: "block", marginTop: 120 }} />;

  async function logout() {
    await api.post("/auth/logout");
    qc.clear();
    router.replace("/login");
  }

  return (
    <div style={{ display: "flex", minHeight: "100vh" }}>
      <aside
        style={{
          width: 216,
          flex: "none",
          background: "#1f1a0a",
          color: "#d6cfb8",
          display: "flex",
          flexDirection: "column",
          padding: "16px 12px",
          position: "sticky",
          top: 0,
          height: "100vh",
        }}
      >
        <div style={{ padding: "4px 8px 18px", color: "#fff", fontWeight: 700, fontSize: 15 }}>
          🛠️ 运营中心
          <div style={{ color: "#9c927a", fontSize: 11, fontWeight: 400 }}>管平台本身，看不到租户的内容</div>
        </div>
        <nav style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          {NAV.map((g, i) => (
            <div key={g.group ?? i} style={{ display: "flex", flexDirection: "column", gap: 3 }}>
              {g.group && <div style={{ color: "#9c927a", fontSize: 11.5, padding: "12px 12px 4px" }}>{g.group}</div>}
              {g.items.map((m) => {
                const on = pathname === m.path || pathname.startsWith(m.path + "/");
                return (
                  <Link
                    key={m.path}
                    href={m.path}
                    style={{
                      padding: "9px 12px",
                      borderRadius: 10,
                      color: on ? "#fff" : "#c9c1a8",
                      background: on ? "rgba(214,170,40,.22)" : undefined,
                      fontWeight: 550,
                      fontSize: 13.5,
                    }}
                  >
                    <span style={{ marginRight: 10 }}>{m.icon}</span>
                    {m.name}
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>
        <div style={{ marginTop: "auto", borderTop: "1px solid rgba(255,255,255,.08)", paddingTop: 12 }}>
          <div style={{ display: "flex", gap: 10, alignItems: "center", padding: "6px 8px" }}>
            <Avatar style={{ background: "#8a6d00" }}>{me.name.slice(-1)}</Avatar>
            <div>
              <div style={{ color: "#f3efe2", fontSize: 13, fontWeight: 600 }}>{me.name}</div>
              <Tag color={me.role === "ADMIN" ? "gold" : "default"} style={{ marginTop: 2 }}>
                {me.role === "ADMIN" ? "运营管理员" : "运营观察员"}
              </Tag>
            </div>
          </div>
          <div style={{ display: "flex", gap: 4, marginTop: 4 }}>
            <Link href="/password" style={{ color: "#9c927a", fontSize: 12.5, padding: "4px 8px" }}>
              改密码
            </Link>
            <Button type="text" size="small" onClick={logout} style={{ color: "#9c927a" }}>
              登出
            </Button>
          </div>
        </div>
      </aside>
      <main style={{ flex: 1, padding: "24px 32px", minWidth: 0 }}>{children}</main>
    </div>
  );
}
