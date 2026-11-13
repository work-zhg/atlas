/**
 * App 用到的 Atlas 接口（都是网页已经在用的，后端不为 App 加接口）。
 *
 * 对话本身的 hooks（会话、消息、发送、审批、取消、轨迹、反馈、模型）直接用 chat-ui 的，这里只放
 * 列表、新建、文件、工作空间、设备这些网页前端自己写的部分。
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { qk } from "@atlas/chat-ui/src/api/keys";
import type { components } from "@atlas/chat-ui/src/api/schema";
import type { Agent, AgentDetail, Page, Thread, ThreadCreate } from "@atlas/chat-ui/src/api/types";
import { useAtlasHttp } from "@atlas/chat-ui/src/client";
import { ApiError, apiUrl, toApiError, type AtlasClientConfig } from "@atlas/chat-ui/src/lib/http";

type S = components["schemas"];
export type WorkspaceOverview = S["WorkspaceOut"];
export type WorkspaceDir = S["WorkspaceDirOut"];
export type WorkspaceSearch = S["WorkspaceSearchOut"];
export type WorkspaceFilesOut = S["WorkspaceFilesOut"];
export type PreviewSession = S["PreviewSessionOut"];
export type DeviceChoice = S["DeviceChoiceOut"];
export type ThreadDevice = S["ThreadDeviceOut"];

// 下面几个接口返回的是自由 dict，不在 OpenAPI 里（同网页 web/src/api/devices.ts、lib/session.ts）

/** 当前主体（GET /v1/me） */
export interface Me {
  id: string;
  name: string;
  account?: string | null;
  dept?: string | null;
  operations: string[];
}

/** 选已有项目：每个允许目录下一层的子目录名 */
export interface ProjectRoots {
  roots: {
    root: string;
    exists: boolean;
    entries: { name: string; path: string; is_repo: boolean }[];
    truncated: boolean;
  }[];
}

/** 本机文件 · 改动（git 仓库里还没提交的） */
export interface DeviceChanges {
  is_repo: boolean;
  entries: {
    path: string;
    /** A 新增 · M 修改 · D 删除 · R 改名 */
    status: "A" | "M" | "D" | "R";
    size?: number;
    modified_at?: number;
  }[];
}

/** 本机文件 · 一层目录 */
export interface DeviceDirPage {
  entries: { name: string; is_dir: boolean; size?: number; modified_at: number }[];
  truncated: boolean;
}

export function useMe() {
  const http = useAtlasHttp();
  return useQuery({ queryKey: ["me"], queryFn: () => http.get<Me>("/v1/me"), staleTime: 60_000 });
}

// ── 智能体（只选不改）

export function useAgents() {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: [...qk.agents.list("enabled"), "mobile"],
    queryFn: () => http.get<Page<Agent>>("/v1/agents", { status: "enabled", limit: 100 }),
    staleTime: 60_000,
  });
}

export function useAgent(agentId: string | undefined) {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: qk.agents.detail(agentId ?? ""),
    queryFn: () => http.get<AgentDetail>(`/v1/agents/${agentId}`),
    enabled: Boolean(agentId),
    staleTime: 60_000,
  });
}

/** 平台的默认智能体。没设、没启用、或我看不到它 → null */
export function useDefaultAgent() {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: ["agents", "default"],
    queryFn: async () => {
      try {
        return await http.get<AgentDetail>("/v1/agents/default");
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
    },
    staleTime: 60_000,
  });
}

/** 智能体配置里首页要用的几项（spec 在接口里是自由 dict） */
export interface AgentSpecView {
  kind?: string;
  model?: { model: string };
  cli?: { cli_type: string; placement?: string; default_project_dir?: string; model_source?: string };
  approval?: { default_mode: "plan" | "manual" | "auto"; allowed_modes: ("plan" | "manual" | "auto")[] };
}

// ── 会话

const UNFINISHED = new Set(["queued", "running", "suspended"]);

/** 会话列表（侧边菜单）。有运行中 / 等你确认的会话时定时刷新状态点 */
export function useThreads() {
  const http = useAtlasHttp();
  return useInfiniteQuery({
    queryKey: qk.threads.list(),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => http.get<Page<Thread>>("/v1/threads", { cursor: pageParam, limit: 30 }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: (query) =>
      query.state.data?.pages.some((p) => p.data.some((t) => t.last_run && UNFINISHED.has(t.last_run.status)))
        ? 10_000
        : false,
  });
}

export function useCreateThread() {
  const http = useAtlasHttp();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ThreadCreate) => http.post<Thread>("/v1/threads", body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.threads.all }),
  });
}

export function useRenameThread(threadId: string) {
  const http = useAtlasHttp();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (title: string) => http.patch<Thread>(`/v1/threads/${threadId}`, { title }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.threads.all }),
  });
}

export function useDeleteThread() {
  const http = useAtlasHttp();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (threadId: string) => http.del<void>(`/v1/threads/${threadId}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.threads.all }),
  });
}

/** 会话工作目录里的文件。运行中定时刷新（CLI 在 Pod 里直接写文件，平台侧没有事件） */
export function useThreadFiles(threadId: string | undefined, running: boolean, enabled = true) {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: ["threads", "files", threadId ?? ""],
    queryFn: () => http.get<WorkspaceFilesOut>(`/v1/threads/${threadId}/files`),
    enabled: Boolean(threadId) && enabled,
    refetchInterval: running ? 10_000 : false,
  });
}

/**
 * 预览站点的入口。★ 令牌在 URL 路径里，后端空闲 1 小时才失效、每次访问都续期：50 分钟内复用同一个。
 * src = "ws" 是整个工作空间；否则是会话 id（根是这个会话的工作目录）。
 */
export function usePreviewSession(src: string | undefined) {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: src === "ws" ? ["workspace", "preview"] : ["threads", "preview", src ?? ""],
    queryFn: () =>
      http.post<PreviewSession>(src === "ws" ? "/v1/workspace/preview" : `/v1/threads/${src}/files/preview`),
    enabled: Boolean(src),
    staleTime: 50 * 60_000,
    refetchOnWindowFocus: false,
  });
}

/** 站点里某个文件的地址。★ 逐段编码：中文、空格、#、? 都要转义，/ 不能转 */
export function previewFileUrl(baseUrl: string, path: string): string {
  return baseUrl + path.split("/").map(encodeURIComponent).join("/");
}

/** 发消息前添加的文件：放进会话工作目录的 uploads/ 下，返回它在工作目录里的路径 */
export async function uploadThreadFile(
  client: AtlasClientConfig,
  threadId: string,
  file: { name: string; body: Blob },
): Promise<string> {
  const res = await fetch(apiUrl(`/v1/threads/${threadId}/files`, { path: `uploads/${file.name}` }, client.apiBase), {
    method: "PUT",
    headers: client.headers?.() ?? {},
    body: file.body,
  });
  if (!res.ok) throw new Error(`「${file.name}」上传失败：${(await toApiError(res)).message}`);
  return ((await res.json()) as { path: string }).path;
}

/** 附件路径并进消息正文：智能体按路径去工作目录里读（同网页） */
export function withAttachments(text: string, paths: string[]): string {
  if (paths.length === 0) return text;
  return `${text}\n\n附件：\n${paths.map((p) => `- ${p}`).join("\n")}`;
}

// ── 工作空间（只看）

export function useWorkspaceOverview(enabled = true) {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: ["workspace", "overview"],
    queryFn: () => http.get<WorkspaceOverview>("/v1/workspace"),
    enabled,
  });
}

export function useWorkspaceDir(dir: string) {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: ["workspace", "dir", dir, false],
    queryFn: () => http.get<WorkspaceDir>("/v1/workspace/entries", { dir }),
  });
}

export function useWorkspaceSearch(q: string) {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: ["workspace", "search", q, false],
    queryFn: () => http.get<WorkspaceSearch>("/v1/workspace/search", { q }),
    enabled: q.trim().length > 0,
  });
}

// ── 本机 CLI（设备）

export function useDeviceChoices(cliType: string | undefined) {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: ["devices", "choices", cliType ?? ""],
    queryFn: () => http.get<DeviceChoice[]>("/v1/devices/choices", { cli_type: cliType }),
    enabled: Boolean(cliType),
    refetchInterval: 15_000,
  });
}

export function useDeviceProjects(deviceId: string | undefined, enabled: boolean) {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: ["devices", "projects", deviceId ?? ""],
    queryFn: () => http.get<ProjectRoots>(`/v1/devices/${deviceId}/projects`),
    enabled: Boolean(deviceId) && enabled,
  });
}

/** 本机会话：电脑上还没提交的改动（打开时经设备网关现读） */
export function useDeviceChanges(threadId: string, enabled: boolean) {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: ["devices", "changes", threadId],
    queryFn: () => http.get<DeviceChanges>(`/v1/threads/${threadId}/device-files/changes`),
    enabled,
  });
}

/** 本机会话：电脑上某一层目录（逐层加载） */
export function useDeviceDir(threadId: string, path: string, enabled: boolean) {
  const http = useAtlasHttp();
  return useQuery({
    queryKey: ["devices", "dir", threadId, path],
    queryFn: () => http.get<DeviceDirPage>(`/v1/threads/${threadId}/device-files/list`, { path }),
    enabled,
  });
}
