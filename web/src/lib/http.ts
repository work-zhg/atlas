/**
 * HTTP 封装 —— 所有 REST 请求的唯一出口。
 *
 * 两件事集中在这里，别在别处重复：
 *   1. 身份注入（X-User-Id）。后端 identity.py 是唯一身份来源，前端也保持单点，
 *      将来换 JWT/SSO 只改 authHeaders()。
 *   2. 错误信封解包。后端统一返回 {"error": {kind, message, details}}（文档 §11），
 *      前端一律转成 ApiError，组件按 kind 分支而不是猜 HTTP 码。
 */

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

/** 后端错误信封（文档 §11） */
export interface ErrorEnvelope {
  error: {
    kind: string;
    message: string;
    details?: Record<string, unknown>;
  };
}

export class ApiError extends Error {
  readonly status: number;
  readonly kind: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, kind: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind;
    this.details = details;
  }

  /** 422 校验错误常来自 spec.validate()，编辑器要就地显示到字段上 */
  get isValidation(): boolean {
    return this.status === 422 || this.kind === "invalid_spec";
  }
}

/**
 * 身份 header。决策 1：不做鉴权 —— 留空时后端回落到 default_user_id。
 * SSE 也要用这份 header，故导出（这正是不用原生 EventSource 的原因）。
 */
export function authHeaders(): Record<string, string> {
  const uid = process.env.NEXT_PUBLIC_USER_ID;
  return uid ? { "X-User-Id": uid } : {};
}

export function apiUrl(path: string, query?: Record<string, string | number | undefined | null>): string {
  const url = new URL(path.startsWith("/") ? path : `/${path}`, API_BASE);
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }
  return url.toString();
}

async function toApiError(res: Response): Promise<ApiError> {
  let kind = "http_error";
  let message = `${res.status} ${res.statusText}`;
  let details: Record<string, unknown> = {};
  try {
    const body = (await res.json()) as Partial<ErrorEnvelope> & { detail?: unknown };
    if (body.error) {
      kind = body.error.kind ?? kind;
      message = body.error.message ?? message;
      details = body.error.details ?? {};
    } else if (body.detail !== undefined) {
      // FastAPI 自带的 422 校验错误走 detail，不是我们的信封
      kind = "validation_error";
      message = "请求参数不合法";
      details = { detail: body.detail };
    }
  } catch {
    // 响应不是 JSON（如 502 网关页），保留状态码信息即可
  }
  return new ApiError(res.status, kind, message, details);
}

export type Query = Record<string, string | number | undefined | null>;

export interface RequestOptions {
  method?: string;
  query?: Query;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = "GET", query, body, headers = {}, signal } = opts;

  const res = await fetch(apiUrl(path, query), {
    method,
    signal,
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...authHeaders(),
      ...headers,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) throw await toApiError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const http = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) =>
    request<T>(path, { query, signal }),
  post: <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
    request<T>(path, { method: "POST", body, headers }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: "PATCH", body }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
