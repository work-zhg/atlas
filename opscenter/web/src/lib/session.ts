import { useQuery } from "@tanstack/react-query";

import { api } from "./api";
import type { Me, SystemInfo } from "./types";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => api.get<Me>("/auth/me"), retry: false, staleTime: 60_000 });
}

export function useSystems() {
  return useQuery({ queryKey: ["systems"], queryFn: () => api.get<SystemInfo[]>("/systems"), staleTime: 300_000 });
}

/** 声明了某种能力的系统（按登记顺序）。 */
export function withCapability(systems: SystemInfo[] | undefined, cap: string): SystemInfo[] {
  return (systems ?? []).filter((s) => s.capabilities.includes(cap));
}

export function systemName(systems: SystemInfo[] | undefined, code: string): string {
  return systems?.find((s) => s.code === code)?.name ?? code;
}
