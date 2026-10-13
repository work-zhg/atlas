"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Popover } from "antd";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { http } from "@/lib/http";
import { can, useMe } from "@/lib/session";

import { Icon, type IconName } from "./Icon";
import styles from "./Rail.module.css";

interface NavItem {
  href: string;
  icon: IconName;
  label: string;
  /** 前缀匹配用 —— /agents/xxx 也应高亮「智能体」 */
  match: string;
  /** 有其中任一操作码才显示（权限设计 §6.2；只是体验，权限在服务端） */
  ops: string[];
}

const NAV: NavItem[] = [
  { href: "/chat", icon: "chat", label: "对话", match: "/chat", ops: ["chat:use"] },
  { href: "/agents", icon: "bot", label: "智能体", match: "/agents", ops: ["chat:use"] },
  {
    href: "/skills",
    icon: "spark",
    label: "技能",
    match: "/skills",
    ops: ["skill:upload", "skill:review", "agent:create", "agent:manage_all"],
  },
  {
    href: "/knowledge",
    icon: "book",
    label: "知识库",
    match: "/knowledge",
    ops: ["kb:create", "kb:manage_all", "kb:review", "agent:create"],
  },
  { href: "/mcp", icon: "plug", label: "MCP", match: "/mcp", ops: ["mcp:manage", "mcp:manage_all"] },
  { href: "/memories", icon: "brain", label: "记忆", match: "/memories", ops: ["chat:use"] },
  {
    href: "/settings/instructions",
    icon: "todo",
    label: "指令",
    match: "/settings/instructions",
    ops: ["chat:use"],
  },
  {
    href: "/settings/service-accounts",
    icon: "shield",
    label: "服务账号",
    match: "/settings/service-accounts",
    ops: ["service_account:manage"],
  },
];

export function Rail() {
  const pathname = usePathname();
  const router = useRouter();
  const qc = useQueryClient();
  const { data: me } = useMe();
  // 文件预览页在新标签里单独打开，是一个整页查看器，不需要主导航；登录页也不要
  if (pathname.startsWith("/files/") || pathname === "/login") return null;

  async function logout() {
    await http.post("/v1/auth/logout");
    qc.clear();
    router.replace("/login");
  }

  return (
    <nav className={styles.rail} aria-label="主导航">
      <div className={styles.logo} title="Atlas">
        A
      </div>

      {NAV.filter((item) => can(me, ...item.ops)).map((item) => {
        const active = pathname === item.match || pathname.startsWith(`${item.match}/`);
        return (
          <Link
            key={item.href}
            href={item.href}
            className={styles.item}
            aria-current={active ? "page" : undefined}
          >
            <Icon name={item.icon} size={20} />
            {/* MASTER.md §1：Rail 图标必须带文字标签，不做纯图标导航 */}
            <span>{item.label}</span>
          </Link>
        );
      })}

      <div className={styles.spacer} />
      <Popover
        placement="rightBottom"
        trigger="click"
        content={
          <div style={{ minWidth: 160, fontSize: 13 }}>
            <div style={{ fontWeight: 600 }}>{me?.name ?? "—"}</div>
            {me?.account && <div style={{ color: "var(--fg-3)" }}>{me.account}</div>}
            {me?.dept && <div style={{ color: "var(--fg-3)" }}>{me.dept}</div>}
            {me?.auth_mode === "uc" ? (
              <a onClick={() => void logout()} style={{ display: "inline-block", marginTop: 8 }}>
                退出登录
              </a>
            ) : (
              <div style={{ color: "var(--fg-3)", marginTop: 8 }}>本机开发模式（未接入登录）</div>
            )}
          </div>
        }
      >
        <div className={styles.avatar} title={me?.name ?? "用户"} style={{ cursor: "pointer" }}>
          {(me?.name ?? "本").slice(-1)}
        </div>
      </Popover>
    </nav>
  );
}
