"use client";

/**
 * 我的设备与本机 CLI 会话（agent-platform/doc/design/device-cli-design.html §2 · §5）。
 *
 * ★ 在线状态来自设备网关的在线表，不缓存太久：列表 15 秒刷新一次，添加设备时 3 秒一次等电脑连上。
 * ★ 本机文件打开时才经设备网关现读（不同步到平台），电脑不在线时接口回 503 + 原因。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { components } from "@atlas/chat-ui/src/api/schema";
import { http } from "@/lib/http";

type S = components["schemas"];
export type Device = S["DeviceOut"];
export type DeviceChoice = S["DeviceChoiceOut"];
export type PairingCode = S["PairingCodeOut"];
export type ThreadDevice = S["ThreadDeviceOut"];

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
    version?: string;
  }[];
}

/** 本机文件 · 一层目录 */
export interface DeviceDirPage {
  entries: { name: string; is_dir: boolean; size?: number; modified_at: number; version?: string }[];
  truncated: boolean;
}

const KEY = ["devices"] as const;

export function useDevices(opts: { pairing?: boolean } = {}) {
  return useQuery({
    queryKey: [...KEY, "list"],
    queryFn: () => http.get<{ data: Device[] }>("/v1/devices"),
    refetchInterval: opts.pairing ? 3_000 : 15_000,
  });
}

export function useStartPairing() {
  return useMutation({ mutationFn: () => http.post<PairingCode>("/v1/devices/pairing") });
}

export function useDeviceActions() {
  const qc = useQueryClient();
  const done = () => void qc.invalidateQueries({ queryKey: KEY });
  return {
    rename: useMutation({
      mutationFn: ({ id, name }: { id: string; name: string }) =>
        http.patch<Device>(`/v1/devices/${id}`, { name }),
      onSuccess: done,
    }),
    setDefault: useMutation({
      mutationFn: (id: string) => http.post<Device>(`/v1/devices/${id}/default`),
      onSuccess: done,
    }),
    revoke: useMutation({
      mutationFn: (id: string) => http.del<void>(`/v1/devices/${id}`),
      onSuccess: done,
    }),
  };
}

/** 新建本机 CLI 对话时可选的电脑：只有在线、装了这种 CLI 的能选 */
export function useDeviceChoices(cliType: string | undefined) {
  return useQuery({
    queryKey: [...KEY, "choices", cliType ?? ""],
    queryFn: () => http.get<DeviceChoice[]>("/v1/devices/choices", { cli_type: cliType }),
    enabled: Boolean(cliType),
    refetchInterval: 15_000,
  });
}

export function useDeviceProjects(deviceId: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: [...KEY, "projects", deviceId ?? ""],
    queryFn: () => http.get<ProjectRoots>(`/v1/devices/${deviceId}/projects`),
    enabled: Boolean(deviceId) && enabled,
    retry: false,
  });
}

/** 本机会话的改动：一轮进行中 10 秒刷新（连接器把结果缓存一两秒，多个标签页也只算一次） */
export function useDeviceChanges(threadId: string, enabled: boolean, running: boolean) {
  return useQuery({
    queryKey: ["threads", "device-changes", threadId],
    queryFn: () => http.get<DeviceChanges>(`/v1/threads/${threadId}/device-files/changes`),
    enabled,
    retry: false,
    refetchInterval: running ? 10_000 : false,
  });
}

/** 本机会话的一层目录：展开时才读 */
export function useDeviceDir(threadId: string, path: string, enabled: boolean) {
  return useQuery({
    queryKey: ["threads", "device-dir", threadId, path],
    queryFn: () => http.get<DeviceDirPage>(`/v1/threads/${threadId}/device-files/list`, { path }),
    enabled,
    retry: false,
  });
}

/** 设备的状态文字与颜色（对话头部、选设备、我的设备共用） */
export function deviceStatusText(status: string | undefined): string {
  switch (status) {
    case "online":
      return "在线";
    case "offline":
      return "不在线";
    case "revoked":
      return "已吊销";
    default:
      return "状态未知";
  }
}
