/**
 * 当前主体（GET /v1/me）：登录态、操作码、菜单（doc/auth-permission-design.html §12–§13）。
 * 菜单与按钮按操作码显隐只是体验，权限在服务端。
 */

import { useQuery } from "@tanstack/react-query";

import { http } from "@/lib/http";

export interface Me {
  auth_mode: "dev" | "uc";
  kind: "user" | "service";
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
