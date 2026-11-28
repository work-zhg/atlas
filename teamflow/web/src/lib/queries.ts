"use client";

/**
 * 读接口的 hooks（react-query）。键按对象分：改了谁就让谁失效。
 *
 *   ["me"]                          我是谁、操作码、是不是系统租户、流程市场级别
 *   ["teams"] / ["team", id, …]     团队、成员、数字员工、模板、项目、通知设置
 *   ["project", id, …]              项目详情（绑定与角色分配）、流程列表
 *   ["process", id, …]              流程详情、事件、流程空间
 *   ["node", 流程, 节点]            节点详情（提交、评审、指令）
 *   ["market", …]                   流程市场
 *   ["names", ids]                  人名
 */
import { useQuery } from "@tanstack/react-query";

import { api } from "./api";
import type {
  AgentCandidate,
  AgentCard,
  MarketTemplate,
  Me,
  NodeDetail,
  Notification,
  ProcessDetail,
  ProcessEvent,
  ProcessRow,
  Project,
  ProjectDetail,
  SpaceListing,
  Team,
  TeamAgent,
  TeamChannels,
  TeamMembers,
  Template,
  Todo,
} from "./types";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => api.get<Me>("/me"), staleTime: 60_000 });
}

export function useTeams() {
  return useQuery({ queryKey: ["teams"], queryFn: () => api.get<Team[]>("/teams") });
}

export function useTeam(id: string | undefined) {
  return useQuery({ queryKey: ["team", id], queryFn: () => api.get<Team>(`/teams/${id}`), enabled: !!id });
}

export function useTeamMembers(id: string | undefined) {
  return useQuery({
    queryKey: ["team", id, "members"],
    queryFn: () => api.get<TeamMembers>(`/teams/${id}/members`),
    enabled: !!id,
  });
}

export function useTeamAgents(id: string | undefined) {
  return useQuery({
    queryKey: ["team", id, "agents"],
    queryFn: () => api.get<TeamAgent[]>(`/teams/${id}/agents`),
    enabled: !!id,
  });
}

export function useAgentCard(team: string | undefined, agent: string | undefined) {
  return useQuery({
    queryKey: ["team", team, "agents", agent],
    queryFn: () => api.get<TeamAgent & { card: AgentCard | null }>(`/teams/${team}/agents/${agent}`),
    enabled: !!team && !!agent,
    staleTime: 60_000,
  });
}

export function useAgentCandidates(team: string | undefined, q: string, enabled: boolean) {
  return useQuery({
    queryKey: ["team", team, "candidates", q],
    queryFn: () => api.get<AgentCandidate[]>(`/teams/${team}/agents/candidates`, { q }),
    enabled: !!team && enabled,
  });
}

export function useTeamChannels(id: string | undefined) {
  return useQuery({
    queryKey: ["team", id, "channels"],
    queryFn: () => api.get<TeamChannels>(`/teams/${id}/channels`),
    enabled: !!id,
  });
}

export function useTemplates(team: string | undefined) {
  return useQuery({
    queryKey: ["team", team, "templates"],
    queryFn: () => api.get<Template[]>(`/teams/${team}/templates`),
    enabled: !!team,
  });
}

export function useTemplate(id: string | undefined) {
  return useQuery({ queryKey: ["template", id], queryFn: () => api.get<Template>(`/templates/${id}`), enabled: !!id });
}

export function useProjects(team: string | undefined) {
  return useQuery({
    queryKey: ["team", team, "projects"],
    queryFn: () => api.get<Project[]>(`/teams/${team}/projects`),
    enabled: !!team,
  });
}

export function useProject(id: string | undefined) {
  return useQuery({
    queryKey: ["project", id],
    queryFn: () => api.get<ProjectDetail>(`/projects/${id}`),
    enabled: !!id,
  });
}

export function useProcesses(project: string | undefined, status: string) {
  return useQuery({
    queryKey: ["project", project, "processes", status],
    queryFn: () => api.get<ProcessRow[]>(`/projects/${project}/processes`, { status: status === "all" ? undefined : status }),
    enabled: !!project,
  });
}

export function useProcess(id: string | undefined) {
  return useQuery({
    queryKey: ["process", id],
    queryFn: () => api.get<ProcessDetail>(`/processes/${id}`),
    enabled: !!id,
  });
}

export function useNode(process: string | undefined, node: string | undefined) {
  return useQuery({
    queryKey: ["node", process, node],
    queryFn: () => api.get<NodeDetail>(`/processes/${process}/nodes/${node}`),
    enabled: !!process && !!node,
  });
}

export function useEvents(process: string | undefined) {
  return useQuery({
    queryKey: ["process", process, "events"],
    queryFn: () => api.get<ProcessEvent[]>(`/processes/${process}/events`),
    enabled: !!process,
  });
}

export function useSpace(process: string | undefined, dir = "") {
  return useQuery({
    queryKey: ["process", process, "space", dir],
    queryFn: () => api.get<SpaceListing>(`/processes/${process}/space`, { dir }),
    enabled: !!process,
  });
}

export function useMarket(category: string) {
  return useQuery({
    queryKey: ["market", "list", category],
    queryFn: () =>
      api.get<{ maintainer: boolean; items: MarketTemplate[] }>("/market", {
        category: category === "all" ? undefined : category,
      }),
  });
}

export function useMarketTemplate(id: string | undefined) {
  return useQuery({
    queryKey: ["market", id],
    queryFn: () => api.get<MarketTemplate>(`/market/${id}`),
    enabled: !!id,
  });
}

export function useTodos() {
  return useQuery({ queryKey: ["todos"], queryFn: () => api.get<Todo[]>("/todos"), refetchInterval: 60_000 });
}

export function useNotifications() {
  return useQuery({
    queryKey: ["notifications"],
    queryFn: () => api.get<{ unread: number; items: Notification[] }>("/notifications"),
    refetchInterval: 30_000,
  });
}

/** 一批人的名字（用户中心）：{uid: {id, name, account}}。缺的人显示 uid 前几位 */
export function useNames(ids: (string | null | undefined)[]) {
  const uniq = [...new Set(ids.filter((x): x is string => !!x))].sort();
  return useQuery({
    queryKey: ["names", uniq.join(",")],
    queryFn: () => api.get<Record<string, { id: string; name: string; account?: string | null }>>("/people/names", { ids: uniq.join(",") }),
    enabled: uniq.length > 0,
    staleTime: 5 * 60_000,
  });
}
