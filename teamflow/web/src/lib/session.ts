"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "./api";
import type { Me, MenuNode } from "./types";

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

/** 落地页：团队（公共菜单，人人可见）。 */
export function landingPath(me: Me): string {
  const leaves = flatMenus(me.menus).filter((m) => m.path);
  return leaves.find((m) => m.path === "/teams")?.path ?? leaves[0]?.path ?? "/teams";
}
