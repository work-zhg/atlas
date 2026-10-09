"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Avatar, Button, Spin } from "antd";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { api, ApiError } from "@/lib/api";
import { flatMenus, landingPath, useMe } from "@/lib/session";
import type { MenuNode } from "@/lib/types";

/** 团队下的页面（项目、项目设置）不是菜单，归属「团队」菜单（权限设计 §06）。 */
const OWNER_MENU: Record<string, string> = { "/projects": "/teams", "/processes": "/teams" };

function menuOf(pathname: string, leaves: MenuNode[]): string | undefined {
  const hit = leaves
    .map((m) => m.path ?? "")
    .filter((p) => p && (pathname === p || pathname.startsWith(p + "/")))
    .sort((a, b) => b.length - a.length)[0];
  if (hit) return hit;
  const top = "/" + (pathname.split("/")[1] ?? "");
  return OWNER_MENU[top];
}

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const qc = useQueryClient();
  const { data: me, error } = useMe();
  const leaves = me ? flatMenus(me.menus) : [];
  const active = menuOf(pathname, leaves);
  const allowed = !!active && leaves.some((m) => m.path === active);

  useEffect(() => {
    if (error instanceof ApiError && error.status === 401) router.replace("/login");
    else if (me && !allowed) router.replace(landingPath(me));
  }, [me, error, allowed, router]);

  if (!me || !allowed) return <Spin style={{ display: "block", marginTop: 120 }} />;

  async function logout() {
    await api.post("/auth/logout");
    qc.clear();
    router.replace("/login");
  }

  const item = (m: MenuNode, indent = false) => {
    const on = m.path === active;
    return (
      <Link
        key={m.code ?? m.name}
        href={m.path ?? "/"}
        style={{
          padding: indent ? "9px 12px 9px 26px" : "10px 12px",
          borderRadius: 10,
          color: on ? "#fff" : "#aeb8cb",
          background: on ? "linear-gradient(90deg, rgba(14,165,233,.26), rgba(99,102,241,.16))" : undefined,
          fontWeight: 550,
          fontSize: 13.5,
          textDecoration: "none",
        }}
      >
        <span style={{ marginRight: 10 }}>{m.icon}</span>
        {m.name}
      </Link>
    );
  };

  return (
    <div style={{ display: "flex", minHeight: "100vh" }}>
      <aside
        style={{
          width: 224,
          flex: "none",
          background: "#111827",
          color: "#cbd5e1",
          display: "flex",
          flexDirection: "column",
          padding: "16px 12px",
          position: "sticky",
          top: 0,
          height: "100vh",
        }}
      >
        <div style={{ padding: "4px 8px 18px", color: "#fff", fontWeight: 700, fontSize: 15 }}>
          🧩 AI TeamFlow
          <div style={{ color: "#8592a8", fontSize: 11, fontWeight: 400 }}>人 + Agent 的研发流程</div>
        </div>
        <nav style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          {me.menus.map((m) =>
            m.children ? (
              <div key={m.name} style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                <div style={{ color: "#8592a8", fontSize: 11.5, padding: "12px 12px 4px" }}>
                  {m.icon} {m.name}
                </div>
                {m.children.map((c) => item(c, true))}
              </div>
            ) : (
              item(m)
            ),
          )}
        </nav>
        <div style={{ marginTop: "auto", borderTop: "1px solid rgba(255,255,255,.08)", paddingTop: 12 }}>
          <div style={{ display: "flex", gap: 10, alignItems: "center", padding: "6px 8px" }}>
            <Avatar style={{ background: "#14b8a6" }}>{me.user.name.slice(-1)}</Avatar>
            <div>
              <div style={{ color: "#eef2f8", fontSize: 13, fontWeight: 600 }}>{me.user.name}</div>
              <div style={{ color: "#8592a8", fontSize: 11 }}>{me.user.account}</div>
            </div>
          </div>
          <Button type="text" size="small" onClick={logout} style={{ color: "#8592a8", marginTop: 4 }}>
            退出登录
          </Button>
        </div>
      </aside>
      <main style={{ flex: 1, minWidth: 0, padding: "24px 28px 60px" }}>{children}</main>
    </div>
  );
}
