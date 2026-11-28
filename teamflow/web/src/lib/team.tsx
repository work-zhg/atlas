"use client";

/**
 * 当前团队：项目、团队两个一级菜单都按它显示（顶栏的团队切换）。
 *
 * ★ 记在本机；进某个项目或流程的页面时跟着切到它所属的团队（我得能看那个团队）。
 * ★ 团队级别、操作码一起给出能不能做某类事（只负责「看不到」，后端接口会再判一次）。
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { atLeast } from "./format";
import { useMe, useTeams } from "./queries";
import type { Level, Me, Team } from "./types";

const KEY = "teamflow.team";

interface TeamState {
  teams: Team[];
  team: Team | undefined;
  setTeam: (id: string) => void;
  loading: boolean;
}

const Ctx = createContext<TeamState | null>(null);

export function TeamProvider({ children }: { children: ReactNode }) {
  const { data: teams, isPending } = useTeams();
  const [picked, setPicked] = useState<string | null>(null);
  useEffect(() => setPicked(localStorage.getItem(KEY)), []);
  const setTeam = useCallback((id: string) => {
    localStorage.setItem(KEY, id);
    setPicked(id);
  }, []);
  const list = useMemo(() => (teams ?? []).filter((t) => t.status === "active"), [teams]);
  const team = list.find((t) => t.id === picked) ?? list[0];
  const value = useMemo(() => ({ teams: list, team, setTeam, loading: isPending }), [list, team, setTeam, isPending]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useCurrentTeam(): TeamState {
  const c = useContext(Ctx);
  if (!c) throw new Error("缺少 <TeamProvider>");
  return c;
}

/** 看某个团队的东西时，把当前团队切过去 */
export function useFollowTeam(teamId: string | undefined) {
  const { team, teams, setTeam } = useCurrentTeam();
  useEffect(() => {
    if (teamId && team?.id !== teamId && teams.some((t) => t.id === teamId)) setTeam(teamId);
  }, [teamId, team?.id, teams, setTeam]);
}

/** 能不能做：操作码 ∧ 团队级别（和后端 access/control.py 的 Action 一致） */
export function can(me: Me | undefined, level: Level | null | undefined) {
  const ops = new Set(me?.operations ?? []);
  return {
    configTeam: ops.has("team:config") && atLeast(level, "OWNER"),
    configFlow: ops.has("flow:config") && atLeast(level, "OWNER"),
    run: ops.has("process:run") && atLeast(level, "WRITE"),
    view: ops.has("process:view") && atLeast(level, "READ"),
    createTeam: ops.has("team:create"),
  };
}

export function useCan() {
  const { data: me } = useMe();
  const { team } = useCurrentTeam();
  return can(me, team?.my_level);
}
