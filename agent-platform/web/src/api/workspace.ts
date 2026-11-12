"use client";

/**
 * 工作空间：一个人的全部文件（后端 {user}/files/）。路径一律相对工作空间的根，根目录是空串。
 *
 * ★ 和会话无关：会话只在建的时候选一个目录当工作目录（ThreadCreate.workspace_dir）。
 * ★ 上传、下载走 fetch：请求体 / 响应是文件原文，不是 JSON。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { components } from "@atlas/chat-ui/src/api/schema";
import { apiUrl, authHeaders, http, request, toApiError } from "@/lib/http";

type S = components["schemas"];
export type WorkspaceOverview = S["WorkspaceOut"];
export type WorkspaceDir = S["WorkspaceDirOut"];
export type WorkspaceFolder = S["WorkspaceFolder"];
export type WorkspaceFile = S["WorkspaceEntryFile"];
export type WorkspaceSearch = S["WorkspaceSearchOut"];
export type WorkspaceRefs = S["WorkspaceRefsOut"];
export type PreviewSession = S["PreviewSessionOut"];

const KEY = ["workspace"] as const;

export function useWorkspaceOverview() {
  return useQuery({
    queryKey: [...KEY, "overview"],
    queryFn: () => http.get<WorkspaceOverview>("/v1/workspace"),
  });
}

export function useWorkspaceDir(dir: string, hidden: boolean) {
  return useQuery({
    queryKey: [...KEY, "dir", dir, hidden],
    queryFn: () => http.get<WorkspaceDir>("/v1/workspace/entries", { dir, hidden: hidden ? "true" : undefined }),
  });
}

export function useWorkspaceSearch(q: string, hidden: boolean) {
  return useQuery({
    queryKey: [...KEY, "search", q, hidden],
    queryFn: () => http.get<WorkspaceSearch>("/v1/workspace/search", { q, hidden: hidden ? "true" : undefined }),
    enabled: q.trim().length > 0,
  });
}

/** 把这个目录（或它下面的目录）当工作目录的对话：改名、删除前给用户看 */
export function fetchWorkspaceRefs(path: string): Promise<WorkspaceRefs> {
  return http.get<WorkspaceRefs>("/v1/workspace/refs", { path });
}

/**
 * 预览站点的入口（与对话里的文件预览同一个站点，根是整个工作空间）。
 * ★ 后端空闲 1 小时才失效、每次访问都续期：50 分钟内复用同一个令牌。
 */
export function useWorkspacePreview(enabled: boolean) {
  return useQuery({
    queryKey: [...KEY, "preview"],
    queryFn: () => http.post<PreviewSession>("/v1/workspace/preview"),
    enabled,
    staleTime: 50 * 60_000,
    refetchOnWindowFocus: false,
  });
}

/** 改了文件之后：列表、占用都重取 */
function useInvalidate() {
  const qc = useQueryClient();
  return () => void qc.invalidateQueries({ queryKey: KEY });
}

export function useMakeFolder() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (path: string) => http.post<{ path: string }>("/v1/workspace/folders", { path }),
    onSuccess: invalidate,
  });
}

export function useMoveEntry() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (v: { src: string; dst: string }) => http.post<{ path: string }>("/v1/workspace/move", v),
    onSuccess: invalidate,
  });
}

export function useDeleteEntry() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (path: string) => request<void>("/v1/workspace/entries", { method: "DELETE", query: { path } }),
    onSuccess: invalidate,
  });
}

/** 上传一个文件到 path（覆盖同名）。onProgress 给 0–1：fetch 拿不到上传进度，用 XHR。 */
export function uploadWorkspaceFile(path: string, file: File, onProgress?: (p: number) => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", apiUrl("/v1/workspace/files", { path }));
    for (const [k, v] of Object.entries(authHeaders())) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress?.(e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve();
      // 错误信封的解包（含 5xx 一律「系统忙」）与其余接口同一套
      void toApiError(new Response(xhr.responseText, { status: xhr.status })).then((e) => reject(e));
    };
    xhr.onerror = () => reject(new Error("网络错误，上传没有完成"));
    xhr.send(file);
  });
}

async function saveAs(url: string, name: string): Promise<void> {
  const res = await fetch(url, { headers: authHeaders() });
  if (!res.ok) throw await toApiError(res);
  const href = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = href;
  a.download = name;
  a.click();
  URL.revokeObjectURL(href);
}

export function downloadWorkspaceFile(path: string): Promise<void> {
  return saveAs(apiUrl("/v1/workspace/files/download", { path }), path.split("/").pop() || "file");
}

/** 把一个文件夹打包成 zip 下载（不含隐藏文件）。path 为空 = 整个工作空间。 */
export function downloadWorkspaceZip(path: string): Promise<void> {
  return saveAs(apiUrl("/v1/workspace/zip", { path }), `${path.split("/").pop() || "工作空间"}.zip`);
}
