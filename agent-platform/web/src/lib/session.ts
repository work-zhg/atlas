/**
 * 当前主体（GET /v1/me）：操作码、菜单（doc/auth-permission-design.html §12–§13）。
 * 菜单与按钮按操作码显隐只是体验，权限在服务端。
 *
 * 登录统一在用户中心（doc/identity-propagation-design.html §4）：网关校验用户中心的会话、把身份
 * 写进请求头；未登录时接口 401，前端整页跳到 /uc/login?next=<当前页>，登录后回来。
 */

import { useQuery } from "@tanstack/react-query";

import { http } from "@/lib/http";

export interface Me {
  id: string;
  name: string;
  account?: string | null;
  email?: string | null;
  dept?: string | null;
  operations: string[];
  menus: { name: string; path?: string | null; children?: unknown[] }[];
}

export const ME_KEY = ["me"] as const;

export function useMe() {
  return useQuery({ queryKey: ME_KEY, queryFn: () => http.get<Me>("/v1/me"), staleTime: 60_000 });
}

export function can(me: Me | undefined, ...ops: string[]): boolean {
  return !!me && ops.some((op) => me.operations.includes(op));
}

/** 用户中心的登录页（与 Atlas 同源，经网关 /uc）。next = 登录后回到的页面（含 basePath）。 */
export function ucLoginUrl(next?: string): string {
  const here =
    next ?? (typeof window === "undefined" ? "/" : window.location.pathname + window.location.search);
  return `/uc/login?next=${encodeURIComponent(here)}`;
}
