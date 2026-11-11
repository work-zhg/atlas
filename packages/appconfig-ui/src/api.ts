/**
 * 应用配置的管理接口 —— 与 infra-platform/libs/appconfig/src/atlas_appconfig/routes.py 一一对应：
 *
 *   GET   {endpoint}                    本版本代码声明的 key：类型、默认值、是否已覆盖、生效值、版本
 *   GET   {endpoint}/{key}
 *   PUT   {endpoint}/{key}              改值（value=null 恢复默认值），带期望版本；不对 409
 *   GET   {endpoint}/{key}/revisions    历史，最新的在前
 *   POST  {endpoint}/{key}/rollback     把某一版的值写成新版本，带期望版本
 *
 * endpoint = 宿主挂 ConfigRoutes 的位置（如 Atlas 是 `${API_BASE}/v1/configs`）。
 * ★ 类型手写、跟着 routes.py 的 ConfigOut / RevisionOut / SetIn / RollbackIn 改：库不属于任何一个宿主，
 *   没有一份可以生成类型的 openapi.json。
 */
import { useQuery } from "@tanstack/react-query";

import type { ConfigValue } from "./value";

export interface ConfigOut {
  key: string;
  /** ValueType.describe()：int、string、list<string>、map<int>…… 用 parseType 解析 */
  type: string;
  description: string;
  default: ConfigValue;
  /** 库里覆盖的值；null = 用默认值 */
  value: ConfigValue | null;
  effective: ConfigValue;
  overridden: boolean;
  version: number;
  updated_by: string;
  updated_at: string;
}

export interface RevisionOut {
  version: number;
  /** 这一版覆盖的值；null = 这一版用默认值（登记、恢复默认） */
  value: ConfigValue | null;
  note: string;
  changed_by: string;
  created_at: string;
}

export interface SetIn {
  value: ConfigValue | null;
  expected_version: number;
  note?: string;
}

export interface RollbackIn {
  to_version: number;
  expected_version: number;
}

/** atlas_appconfig.model.SYSTEM：登记时记的操作人 */
export const SYSTEM = "system";

/** 接口在哪、每次请求带什么头。身份由宿主的会话 Cookie / 网关负责，组件不读环境变量。 */
export interface AppConfigClient {
  endpoint: string;
  headers?: () => Record<string, string>;
}

export class AppConfigError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "AppConfigError";
    this.status = status;
  }

  /** 别人先改了（期望版本对不上） */
  get conflict(): boolean {
    return this.status === 409;
  }
}

async function toError(res: Response): Promise<AppConfigError> {
  let message = `${res.status} ${res.statusText}`;
  try {
    const body = (await res.json()) as { detail?: unknown; message?: unknown; error?: { message?: unknown } };
    if (typeof body.detail === "string") {
      // atlas_appconfig 自己的错误：HTTPException(404 / 409 / 422, 人话)
      message = body.detail;
    } else if (typeof body.message === "string") {
      // 宿主的错误体（如 TeamFlow 鉴权失败：{code, message, details}）
      message = body.message;
    } else if (typeof body.error?.message === "string") {
      // 宿主的错误信封（如 Atlas 鉴权失败：{"error": {kind, message, details}}）
      message = body.error.message;
    } else if (body.detail !== undefined) {
      // FastAPI 自带的请求体校验错误
      message = "请求参数不合法";
    }
  } catch {
    // 响应不是 JSON（如 502 网关页），保留状态码即可
  }
  return new AppConfigError(res.status, message);
}

async function call<T>(client: AppConfigClient, method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${client.endpoint.replace(/\/+$/, "")}${path}`, {
    method,
    credentials: "same-origin",
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(client.headers?.() ?? {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as T;
}

export interface AppConfigApi {
  endpoint: string;
  list: () => Promise<ConfigOut[]>;
  get: (key: string) => Promise<ConfigOut>;
  set: (key: string, body: SetIn) => Promise<ConfigOut>;
  revisions: (key: string) => Promise<RevisionOut[]>;
  rollback: (key: string, body: RollbackIn) => Promise<ConfigOut>;
}

export function appConfigApi(client: AppConfigClient): AppConfigApi {
  const at = (key: string) => `/${encodeURIComponent(key)}`;
  return {
    endpoint: client.endpoint,
    list: () => call(client, "GET", ""),
    get: (key) => call(client, "GET", at(key)),
    set: (key, body) => call(client, "PUT", at(key), body),
    revisions: (key) => call(client, "GET", `${at(key)}/revisions`),
    rollback: (key, body) => call(client, "POST", `${at(key)}/rollback`, body),
  };
}

/** 查询键带上 endpoint：同一页面嵌两个应用的配置也不会串缓存。 */
export const qk = {
  all: (endpoint: string) => ["atlas-appconfig", endpoint] as const,
  list: (endpoint: string) => ["atlas-appconfig", endpoint, "list"] as const,
  revisions: (endpoint: string, key: string) => ["atlas-appconfig", endpoint, "revisions", key] as const,
};

/** 4xx 是业务结论（没权限、不存在），重试只会让人多等；不依赖宿主 QueryClient 的重试策略。 */
function retry(failureCount: number, error: Error): boolean {
  if (error instanceof AppConfigError && error.status < 500) return false;
  return failureCount < 2;
}

export function useConfigs(api: AppConfigApi) {
  // 配置的意义就是「改完立即生效」：每次进页面都重新取，不用宿主默认的缓存时长
  return useQuery({ queryKey: qk.list(api.endpoint), queryFn: api.list, staleTime: 0, retry });
}

export function useRevisions(api: AppConfigApi, key: string, enabled: boolean) {
  return useQuery({
    queryKey: qk.revisions(api.endpoint, key),
    queryFn: () => api.revisions(key),
    enabled,
    staleTime: 0,
    retry,
  });
}
