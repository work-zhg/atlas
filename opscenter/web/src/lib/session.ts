import { useQuery } from "@tanstack/react-query";

import { api } from "./api";
import type { Level, Me, SystemInfo } from "./types";

/** 当前运营人员（网关写的身份 + 用户中心里的授权）。401 = 没登录；403 NOT_OPERATOR = 不是系统租户的人。 */
export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => api.get<Me>("/me"), retry: false, staleTime: 30_000 });
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

/** 租户模块的数据编码 */
export const TENANT_MODULE = "OpsTenant";

const RANK: Record<Level, number> = { NONE: 0, READ: 1, WRITE: 2, OWNER: 3 };

/**
 * 按模块判能做什么（c4-model/opscenter/design-opscenter.html 原理 2）：看要只读，改要读写，
 * 不可恢复的要 Owner。只管显隐和置灰，接口会再判一次。
 */
export interface Access {
  level: (module: string) => Level;
  read: (module: string) => boolean;
  write: (module: string) => boolean;
  owner: (module: string) => boolean;
  /** 某个系统的运营模块 */
  moduleOf: (system: string) => string;
  canAudit: boolean;
}

export function access(me: Me | undefined, systems: SystemInfo[] | undefined): Access {
  const levels = new Map((me?.modules ?? []).map((m) => [m.code, m.level]));
  const level = (module: string): Level => levels.get(module) ?? "NONE";
  const at = (module: string, need: Level) => RANK[level(module)] >= RANK[need];
  const bySystem = new Map((me?.modules ?? []).filter((m) => m.system).map((m) => [m.system!, m.code]));
  return {
    level,
    read: (m) => at(m, "READ"),
    write: (m) => at(m, "WRITE"),
    owner: (m) => at(m, "OWNER"),
    moduleOf: (system) => bySystem.get(system) ?? systems?.find((s) => s.code === system)?.module ?? "",
    canAudit: !!me?.can_audit,
  };
}

export function useAccess(): Access {
  const { data: me } = useMe();
  const { data: systems } = useSystems();
  return access(me, systems);
}
