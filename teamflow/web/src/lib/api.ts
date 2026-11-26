import { csrfHeaders, loginUrl, logout, refreshSession } from "@atlas/chat-ui/src/lib/session";

/**
 * 请求的唯一出口。
 *
 * 1. 同源：接口经 API 网关（/teamflow/api/…）转到 TeamFlow 后端。登录统一在用户中心：网关本地验
 *    HttpOnly Cookie 里的访问 JWT，把身份写进请求头。令牌过期（401）先单飞刷新、重放一次；
 *    刷新也不行才 401 → ucLoginUrl()（@atlas/chat-ui 的 lib/session.ts）。
 * 2. 写请求带双提交的 CSRF 头（值取自非 HttpOnly 的 uc_csrf Cookie，网关校验）。
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
export const writeHeaders = csrfHeaders;

/** 统一登录页（同源）。next = 登录后回到的页面（含 basePath）。 */
export const ucLoginUrl = loginUrl;

/** 登出在用户中心（几个前端共用一个登录态）：作废这次登录后去统一登录页。 */
export const logoutToUC = logout;

/** 部署子路径（与 next.config 的 basePath 一致）。fetch / EventSource / <a href> 不会自动加，要手动拼。 */
export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

/**
 * 接口的同源前缀：经 API 网关是 /teamflow（页面在 /teamflow-ui，前后端名字不同）；
 * 本地开发不设，走页面自己的 /api（next.config 转给后端）。
 */
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

/** 发请求；访问令牌过期（401）就单飞刷新、重放一次。 */
async function send(make: () => Promise<Response>): Promise<Response> {
  const res = await make();
  if (res.status === 401 && (await refreshSession())) return make();
  return res;
}

async function request<T>(method: string, path: string, body?: unknown, query?: Query): Promise<T> {
  const res = await send(() => {
    const headers: Record<string, string> = method === "GET" ? {} : csrfHeaders();
    if (body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(url(path, query), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
    });
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
  // ★ CSRF 头和别的写请求一样是 X-UC-CSRF（网关校验）；原来写成 X-TF-CSRF，上传一律被拒
  const res = await send(() =>
    fetch(`${API_BASE}/api/v1${path}`, {
      method: "POST",
      body: form,
      headers: csrfHeaders(),
      credentials: "same-origin",
    }),
  );
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
