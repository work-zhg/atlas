"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Avatar, Button, Spin } from "antd";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { api, ApiError } from "@/lib/api";
import { flatMenus, landingPath, useMe } from "@/lib/session";

/** 管理台外壳：侧边栏由「用户中心」内置应用的菜单 + 登录人的角色生成（应用接入设计 §11）。 */
export default function ConsoleLayout({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const qc = useQueryClient();
  const { data: me, error } = useMe();
  const menus = me ? flatMenus(me.menus) : [];
  const top = "/" + (pathname.split("/")[1] ?? "");
  const allowed = top === "/me" || menus.some((m) => m.path === top);

  useEffect(() => {
    if (error instanceof ApiError && error.status === 401) router.replace("/login");
    else if (error instanceof ApiError && error.code === "PASSWORD_CHANGE_REQUIRED") router.replace("/change-password");
    else if (me?.must_change_password) router.replace("/change-password");
    else if (me && !allowed) router.replace(landingPath(me));
  }, [me, error, allowed, router]);

  if (!me || me.must_change_password || !allowed) return <Spin style={{ display: "block", marginTop: 120 }} />;

  async function logout() {
    await api.post("/auth/logout");
    qc.clear();
    router.replace("/login");
  }

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
          👥 用户中心
          <div style={{ color: "#8592a8", fontSize: 11, fontWeight: 400 }}>用户 · 组织 · 权限</div>
        </div>
        <nav style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          {menus.map((m) => {
            const active = top === m.path;
            return (
              <Link
                key={m.code}
                href={m.path ?? "/"}
                style={{
                  padding: "10px 12px",
                  borderRadius: 10,
                  color: active ? "#fff" : "#aeb8cb",
                  background: active ? "linear-gradient(90deg, rgba(14,165,233,.26), rgba(99,102,241,.16))" : undefined,
                  fontWeight: 550,
                  fontSize: 13.5,
                  textDecoration: "none",
                }}
              >
                <span style={{ marginRight: 10 }}>{m.icon}</span>
                {m.name}
              </Link>
            );
          })}
        </nav>
        <div style={{ marginTop: "auto", borderTop: "1px solid rgba(255,255,255,.08)", paddingTop: 12 }}>
          <Link href="/me" style={{ display: "flex", gap: 10, alignItems: "center", textDecoration: "none", padding: "6px 8px" }}>
            <Avatar style={{ background: "#14b8a6" }}>{me.user.name.slice(-1)}</Avatar>
            <div>
              <div style={{ color: "#eef2f8", fontSize: 13, fontWeight: 600 }}>{me.user.name}</div>
              <div style={{ color: "#8592a8", fontSize: 11 }}>{me.user.account}</div>
            </div>
          </Link>
          <Button type="text" size="small" onClick={logout} style={{ color: "#8592a8", marginTop: 4 }}>
            退出登录
          </Button>
        </div>
      </aside>
      <main style={{ flex: 1, minWidth: 0, padding: "24px 28px 60px" }}>{children}</main>
    </div>
  );
}
