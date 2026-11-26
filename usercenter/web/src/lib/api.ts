/**
 * 请求的唯一出口。
 *
 * 1. 同源：部署时经 API 网关的 /uc/api/…（页面在 /uc-ui，前后端名字不同）；本地开发走页面自己的 /api
 *    （Next 代理到用户中心后端）。登录态是 HttpOnly Cookie 里的访问 JWT（15 分钟）：接口回 401 先用
 *    刷新令牌换一个新的、重放一次，刷新也不行才算没登录（c4-model/apigateway/dynamic-web-jwt.html）。
 * 2. 写请求带双提交的 CSRF 头（值取自非 HttpOnly 的 uc_csrf Cookie）。
 * 3. 错误统一为 {code, message, details}（总体设计 §10），转成 ApiError。
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

function csrf(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const hit = document.cookie.split("; ").find((c) => c.startsWith("uc_csrf="));
  return hit ? decodeURIComponent(hit.slice("uc_csrf=".length)) : undefined;
}

/** 部署子路径（与 next.config 的 basePath 一致）。fetch / EventSource / <a href> 不会自动加，要手动拼。 */
export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

/** 接口的同源前缀：经 API 网关是 /uc；本地开发不设，走页面自己的 /api。 */
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? BASE_PATH;

type Query = Record<string, string | number | boolean | undefined | null>;

function url(path: string, query?: Query): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const s = qs.toString();
  return `${API_BASE}/api/v1${path}${s ? `?${s}` : ""}`;
}

/** 这几个接口回 401 是「凭据不对 / 刷新令牌也不能用」，不能再拿刷新去重试 */
const NO_REFRESH = new Set(["/auth/login", "/auth/refresh", "/auth/logout"]);

let refreshing: Promise<boolean> | null = null;

/** 用刷新令牌换一个新的访问令牌。单飞：同一时刻多个请求撞上 401 只刷新一次。 */
function refreshSession(): Promise<boolean> {
  if (!refreshing) {
    const headers: Record<string, string> = {};
    const token = csrf();
    if (token) headers["X-UC-CSRF"] = token;
    refreshing = fetch(url("/auth/refresh"), { method: "POST", headers, credentials: "same-origin" })
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

async function request<T>(method: string, path: string, body?: unknown, query?: Query): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") {
    const token = csrf();
    if (token) headers["X-UC-CSRF"] = token;
  }
  const send = () =>
    fetch(url(path, query), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
    });
  let res = await send();
  if (res.status === 401 && !NO_REFRESH.has(path) && (await refreshSession())) res = await send();
  if (!res.ok) {
    let payload: { code?: string; message?: string; details?: Record<string, unknown> } = {};
    try {
      payload = await res.json();
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new ApiError(res.status, payload.code ?? `HTTP_${res.status}`, payload.message ?? res.statusText, payload.details);
  }
  return (await res.json()) as T;
}

export const api = {
  get: <T,>(path: string, query?: Query) => request<T>("GET", path, undefined, query),
  post: <T,>(path: string, body?: unknown, query?: Query) => request<T>("POST", path, body ?? {}, query),
  patch: <T,>(path: string, body: unknown) => request<T>("PATCH", path, body),
  put: <T,>(path: string, body: unknown) => request<T>("PUT", path, body),
  del: <T,>(path: string, query?: Query) => request<T>("DELETE", path, undefined, query),
};

export function errorText(e: unknown): string {
  return e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
}
