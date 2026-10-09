/**
 * 请求的唯一出口。
 *
 * 1. 同源：/api 由 Next 代理到用户中心后端，会话靠 HttpOnly Cookie。
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

type Query = Record<string, string | number | boolean | undefined | null>;

function url(path: string, query?: Query): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const s = qs.toString();
  return `/api/v1${path}${s ? `?${s}` : ""}`;
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
