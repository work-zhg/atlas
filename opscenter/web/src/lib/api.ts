/**
 * 请求的唯一出口：同源的 /api 经 Next 转到运营服务。
 *
 * 1. 登录态是运营服务的 HttpOnly 会话 Cookie；写请求带双提交的 CSRF 头（值取自 ops_csrf Cookie）。
 * 2. 错误统一成 ApiError。运营中心自己的错误是 {code, message}；经「系统代理」转回来的各系统错误
 *    形状不一（用户中心 {code, message}、Atlas {error: {message}}、知识库 {detail}），这里都认。
 * 3. sys(code) 调某个系统的运营接口：/api/systems/{code}/ops/…（运营服务签名后转发）。
 */

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

function csrf(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const hit = document.cookie.split("; ").find((c) => c.startsWith("ops_csrf="));
  return hit ? decodeURIComponent(hit.slice("ops_csrf=".length)) : undefined;
}

/** 写请求要带的头（共享面板 @atlas/appconfig-ui、@atlas/jobs-ui 也用它）。 */
export function writeHeaders(): Record<string, string> {
  const token = csrf();
  return token ? { "X-Ops-CSRF": token } : {};
}

type Query = Record<string, string | number | boolean | undefined | null>;

export function apiUrl(path: string, query?: Query): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const s = qs.toString();
  return `${BASE_PATH}/api${path}${s ? `?${s}` : ""}`;
}

async function toError(res: Response): Promise<ApiError> {
  let p: Record<string, unknown> = {};
  try {
    p = await res.json();
  } catch {
    /* 不是 JSON */
  }
  const err = p.error as { message?: string; kind?: string } | undefined;
  const message =
    (typeof p.message === "string" && p.message) ||
    (typeof p.detail === "string" && p.detail) ||
    err?.message ||
    `请求失败（${res.status}）`;
  const code = (typeof p.code === "string" && p.code) || err?.kind || `HTTP_${res.status}`;
  return new ApiError(res.status, code, message, (p.details as Record<string, unknown>) ?? {});
}

export async function request<T>(method: string, path: string, body?: unknown, query?: Query): Promise<T> {
  const headers: Record<string, string> = method === "GET" ? {} : writeHeaders();
  let payload: BodyInit | undefined;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch(apiUrl(path, query), {
    method,
    headers,
    body: payload,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!res.ok) throw await toError(res);
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const api = {
  get: <T>(path: string, query?: Query) => request<T>("GET", path, undefined, query),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body ?? {}),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body ?? {}),
  del: <T>(path: string, query?: Query) => request<T>("DELETE", path, undefined, query),
};

/** 某个系统的运营接口（经运营服务的系统代理）。 */
export function sys(code: string) {
  const p = (path: string) => `/systems/${code}/ops${path}`;
  return {
    endpoint: apiUrl(p("")),
    get: <T>(path: string, query?: Query) => api.get<T>(p(path), query),
    post: <T>(path: string, body?: unknown) => api.post<T>(p(path), body),
    put: <T>(path: string, body?: unknown) => api.put<T>(p(path), body),
    patch: <T>(path: string, body?: unknown) => api.patch<T>(p(path), body),
    del: <T>(path: string, query?: Query) => api.del<T>(p(path), query),
  };
}

export const UC = "usercenter";
