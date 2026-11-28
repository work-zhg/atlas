import { csrfHeaders, loginUrl, logout, refreshSession } from "@atlas/chat-ui/src/lib/session";

/**
 * 请求的唯一出口。
 *
 * 1. 同源：接口经 API 网关（/teamflow/api/…）转到 TeamFlow 后端。登录统一在用户中心：网关本地验
 *    HttpOnly Cookie 里的访问 JWT，把身份写进请求头。令牌过期（401）先单飞刷新、重放一次；
 *    刷新也不行才 401 → ucLoginUrl()（@atlas/chat-ui 的 lib/session.ts）。
 * 2. 写请求带双提交的 CSRF 头（值取自非 HttpOnly 的 uc_csrf Cookie，网关校验）。
 * 3. 错误统一为 {code, message, details}，转成 ApiError。
 *
 * ★ 节点对话直连 Agent 平台（ATLAS_API_BASE），不经 TeamFlow；只有开新一轮经 TeamFlow。
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

/** 写请求须带的头（双提交 CSRF）。 */
export const writeHeaders = csrfHeaders;

/** 统一登录页（同源）。next = 登录后回到的页面（含 basePath）。 */
export const ucLoginUrl = loginUrl;

/** 登出在用户中心（几个前端共用一个登录态）：作废这次登录后去统一登录页。 */
export const logoutToUC = logout;

export { refreshSession };

/** 部署子路径（与 next.config 的 basePath 一致）。fetch / <a href> 不会自动加，要手动拼。 */
export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

/**
 * 接口的同源前缀：经 API 网关是 /teamflow（页面在 /teamflow-ui，前后端名字不同）；
 * 本地开发不设，走页面自己的 /api（next.config 转给后端）。
 */
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? BASE_PATH;

/** Agent 平台的接口（经网关 /atlas）：节点对话组件直连它。 */
export const ATLAS_API_BASE = process.env.NEXT_PUBLIC_ATLAS_API_BASE ?? "/atlas";

/** Agent 平台的页面（经网关 /atlas-ui）：「在 Atlas 里编辑智能体」之类的链接。 */
export const ATLAS_UI_BASE = process.env.NEXT_PUBLIC_ATLAS_UI_BASE ?? "/atlas-ui";

type Query = Record<string, string | number | boolean | undefined | null>;

export function apiUrl(path: string, query?: Query): string {
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

async function toError(res: Response): Promise<ApiError> {
  let payload: { code?: string; message?: string; details?: Record<string, unknown> } = {};
  try {
    payload = await res.json();
  } catch {
    /* 非 JSON 错误体（网关的 413 之类） */
  }
  const message = res.status === 413 ? "文件太大，传不上去" : (payload.message ?? res.statusText);
  return new ApiError(res.status, payload.code ?? `HTTP_${res.status}`, message, payload.details);
}

async function request<T>(method: string, path: string, body?: unknown, query?: Query): Promise<T> {
  const res = await send(() => {
    const headers: Record<string, string> = method === "GET" ? {} : csrfHeaders();
    if (body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(apiUrl(path, query), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
    });
  });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as T;
}

export const api = {
  get: <T,>(path: string, query?: Query) => request<T>("GET", path, undefined, query),
  post: <T,>(path: string, body?: unknown, query?: Query) => request<T>("POST", path, body ?? {}, query),
  patch: <T,>(path: string, body: unknown) => request<T>("PATCH", path, body),
  put: <T,>(path: string, body: unknown) => request<T>("PUT", path, body),
  del: <T,>(path: string, query?: Query) => request<T>("DELETE", path, undefined, query),
};

/** 把一个文件原样传上去（请求体就是文件内容）。 */
export async function putFile<T>(path: string, file: Blob, query?: Query): Promise<T> {
  const res = await send(() =>
    fetch(apiUrl(path, query), {
      method: "PUT",
      body: file,
      headers: { ...csrfHeaders(), "Content-Type": "application/octet-stream" },
      credentials: "same-origin",
    }),
  );
  if (!res.ok) throw await toError(res);
  return (await res.json()) as T;
}

export function errorText(e: unknown): string {
  return e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
}
