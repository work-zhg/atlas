"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "./api";
import type { MenuNode, Me } from "./types";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => api.get<Me>("/auth/me"), staleTime: 60_000 });
}

/** 前端按操作码控制按钮：只负责「看不到」，后端接口会再鉴权一次。 */
export function useCan(): (op: string) => boolean {
  const { data } = useMe();
  const set = new Set(data?.permissions ?? []);
  return (op: string) => set.has(op);
}

export function flatMenus(nodes: MenuNode[]): MenuNode[] {
  return nodes.flatMap((n) => (n.children ? flatMenus(n.children) : [n]));
}

/** 落地页：第一个非公共菜单；只有公共菜单的人去个人中心。 */
export function landingPath(me: Me): string {
  const leaves = flatMenus(me.menus).filter((m) => m.path && m.code !== "grants");
  return leaves[0]?.path ?? "/me";
}

/**
 * 统一登录（doc/identity-propagation-design.html §4）：Atlas、TeamFlow 未登录时跳到
 * /uc/login?next=<原页面>。登录（或改完密码）后回到 next。
 * ★ 只接受同源的绝对路径（防开放重定向）：以 / 开头、不是 //、也不是协议相对或反斜杠形式。
 */
export function nextTarget(): string | undefined {
  if (typeof window === "undefined") return undefined;
  const raw = new URLSearchParams(window.location.search).get("next");
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) return undefined;
  return raw;
}

/** 带上 next 的站内路径（登录 → 改密时不丢回跳地址）。 */
export function withNext(path: string): string {
  const next = nextTarget();
  return next ? `${path}?next=${encodeURIComponent(next)}` : path;
}

/** 回到 next（跨应用：整页跳转，不走 Next 路由）；没有 next 返回 false。 */
export function goNext(): boolean {
  const next = nextTarget();
  if (!next) return false;
  window.location.assign(next);
  return true;
}
