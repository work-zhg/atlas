/**
 * 请求的唯一出口：经 API 网关的 /opscenter/api/…（页面在 /opscenter-ui，前后端名字不同）；
 * 本地开发走页面自己的 /api（next.config 转给运营服务）。
 *
 * 1. 登录统一在用户中心（c4-model/opscenter/design-opscenter.html）：HttpOnly Cookie 里的访问 JWT，
 *    网关验签后写身份头。令牌过期（401）先单飞刷新、重放一次；写请求带双提交的 CSRF 头
 *    X-UC-CSRF（值取自 uc_csrf Cookie，网关校验）。
 * 2. 错误统一成 ApiError。运营中心自己的错误是 {code, message}；经「系统代理」转回来的各系统错误
 *    形状不一（用户中心 {code, message}、Atlas {error: {message}}、知识库 {detail}），这里都认。
 * 3. sys(code) 调某个系统的运营接口：/api/systems/{code}/proxy/…（运营服务签名后转发）。
 *    ★ 路径里不能有 /ops/ 段：网关对含 /ops/ 段的路径一律 404。
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

/** 运营服务接口的同源前缀：经 API 网关是 /opscenter；本地开发不设，走页面自己的 /api。 */
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? BASE_PATH;
/** 用户中心接口（刷新、登出）与统一登录页，都经 API 网关 */
const UC_API_BASE = process.env.NEXT_PUBLIC_UC_API_BASE ?? "/uc";
const LOGIN_PATH = process.env.NEXT_PUBLIC_LOGIN_PATH ?? "/uc-ui/login";

function csrf(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const hit = document.cookie.split("; ").find((c) => c.startsWith("uc_csrf="));
  return hit ? decodeURIComponent(hit.slice("uc_csrf=".length)) : undefined;
}

/** 写请求要带的头（共享面板 @atlas/appconfig-ui、@atlas/jobs-ui 也用它）。 */
export function writeHeaders(): Record<string, string> {
  const token = csrf();
  return token ? { "X-UC-CSRF": token } : {};
}

let refreshing: Promise<boolean> | null = null;

/** 用刷新令牌换一个新的访问令牌。单飞：同一时刻多个请求撞上 401 只刷新一次。 */
export function refreshSession(): Promise<boolean> {
  if (!refreshing) {
    refreshing = fetch(`${UC_API_BASE}/api/v1/auth/refresh`, {
      method: "POST",
      headers: writeHeaders(),
      credentials: "same-origin",
    })
      .then((r) => r.ok)
      .catch(() => false)
      .finally(() => {
        setTimeout(() => {
          refreshing = null;
        }, 1000);
      });
  }
  return refreshing;
}

/** 统一登录页：登录后回到当前页面 */
export function loginUrl(): string {
  const here = typeof window === "undefined" ? "/" : window.location.pathname + window.location.search;
  return `${LOGIN_PATH}?next=${encodeURIComponent(here)}`;
}

/** 退出登录：作废这次登录（用户中心删刷新令牌、清 Cookie），去统一登录页 */
export async function logout(): Promise<void> {
  await fetch(`${UC_API_BASE}/api/v1/auth/logout`, {
    method: "POST",
    headers: writeHeaders(),
    credentials: "same-origin",
  }).catch(() => undefined);
  window.location.assign(LOGIN_PATH);
}

type Query = Record<string, string | number | boolean | undefined | null>;

export function apiUrl(path: string, query?: Query): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const s = qs.toString();
  return `${API_BASE}/api${path}${s ? `?${s}` : ""}`;
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
  const send = () =>
    fetch(apiUrl(path, query), {
      method,
      headers,
      body: payload,
      credentials: "same-origin",
      cache: "no-store",
    });
  let res = await send();
  // 访问令牌过期（15 分钟）：刷新后重放一次；刷新也不行，401 交给布局跳统一登录页
  if (res.status === 401 && (await refreshSession())) res = await send();
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
  const p = (path: string) => `/systems/${code}/proxy${path}`;
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
