"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { Icon, type IconName } from "./Icon";
import styles from "./Rail.module.css";

interface NavItem {
  href: string;
  icon: IconName;
  label: string;
  /** 前缀匹配用 —— /agents/xxx 也应高亮「智能体」 */
  match: string;
}

const NAV: NavItem[] = [
  { href: "/chat", icon: "chat", label: "对话", match: "/chat" },
  { href: "/agents", icon: "bot", label: "智能体", match: "/agents" },
  { href: "/skills", icon: "spark", label: "技能", match: "/skills" },
  { href: "/mcp", icon: "plug", label: "MCP", match: "/mcp" },
  { href: "/memories", icon: "brain", label: "记忆", match: "/memories" },
];

export function Rail() {
  const pathname = usePathname();
  // 文件预览页在新标签里单独打开，是一个整页查看器，不需要主导航
  if (pathname.startsWith("/files/")) return null;

  return (
    <nav className={styles.rail} aria-label="主导航">
      <div className={styles.logo} title="Atlas">
        A
      </div>

      {NAV.map((item) => {
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
      <div className={styles.avatar} title="本地用户">
        本
      </div>
    </nav>
  );
}
