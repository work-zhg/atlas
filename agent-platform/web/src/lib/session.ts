/**
 * 当前主体（GET /v1/me）：操作码、菜单（doc/auth-permission-design.html §12–§13）。
 * 菜单与按钮按操作码显隐只是体验，权限在服务端。
 *
 * 登录统一在用户中心：API 网关本地验访问 JWT、把身份写进请求头；令牌过期时请求层先刷新再重放，
 * 刷新也不行（接口仍 401）才整页跳到统一登录页（/uc-ui/login?next=<当前页>），登录后回来。
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

/** 统一登录页（用户中心前端，与 Atlas 同源）。next = 登录后回到的页面（含 basePath）。 */
export { loginUrl as ucLoginUrl } from "@atlas/chat-ui/src/lib/session";
