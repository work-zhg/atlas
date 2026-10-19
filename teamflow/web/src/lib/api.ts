/**
 * 请求的唯一出口。
 *
 * 1. 同源：/api 经网关转到 TeamFlow 后端。登录统一在用户中心（doc/identity-propagation-design.html
 *    §4）：网关校验用户中心的 HttpOnly 会话 Cookie，把身份写进请求头；未登录 401 → ucLoginUrl()。
 * 2. 写请求带双提交的 CSRF 头（值取自非 HttpOnly 的 uc_csrf Cookie，由网关交给用户中心校验）。
 * 3. 错误统一为 {code, message, details}，转成 ApiError。
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

/** 写请求须带的头（双提交 CSRF）。嵌入的 Atlas 对话组件经代理发消息时也用它。 */
export function writeHeaders(): Record<string, string> {
  const token = csrf();
  return token ? { "X-UC-CSRF": token } : {};
}

function csrf(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const hit = document.cookie.split("; ").find((c) => c.startsWith("uc_csrf="));
  return hit ? decodeURIComponent(hit.slice("uc_csrf=".length)) : undefined;
}

/** 用户中心的登录页（同源，经网关 /uc）。next = 登录后回到的页面（含 basePath）。 */
export function ucLoginUrl(next?: string): string {
  const here =
    next ?? (typeof window === "undefined" ? "/" : window.location.pathname + window.location.search);
  return `/uc/login?next=${encodeURIComponent(here)}`;
}

/** 登出在用户中心（三个前端共用一个登录态）：登出后去用户中心登录页。 */
export async function logoutToUC(): Promise<void> {
  await fetch("/uc/api/v1/auth/logout", { method: "POST", headers: writeHeaders(), credentials: "same-origin" });
  window.location.assign("/uc/login");
}

/** 部署子路径（与 next.config 的 basePath 一致）。fetch / EventSource / <a href> 不会自动加，要手动拼。 */
export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

type Query = Record<string, string | number | boolean | undefined | null>;

function url(path: string, query?: Query): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const s = qs.toString();
  return `${BASE_PATH}/api/v1${path}${s ? `?${s}` : ""}`;
}

async function request<T>(method: string, path: string, body?: unknown, query?: Query): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") {
    const token = csrf();
    if (token) headers["X-UC-CSRF"] = token;
  }
  const res = await fetch(url(path, query), {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
    cache: "no-store",
  });
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

/** multipart 上传（文件模板）。 */
export async function upload<T>(path: string, form: FormData): Promise<T> {
  const headers: Record<string, string> = {};
  const token = csrf();
  if (token) headers["X-TF-CSRF"] = token;
  const res = await fetch(`${BASE_PATH}/api/v1${path}`, { method: "POST", body: form, headers, credentials: "same-origin" });
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
