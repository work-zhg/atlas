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
/**
 * 配置服务（atlas-config：技能与 MCP 注册表）。BFF 上线前浏览器直连它
 * （doc/skill-mcp-backend-design.html §13.3）；上线后改成同源路径，只换这一处。
 */
export const CONFIG_API_BASE =
  process.env.NEXT_PUBLIC_CONFIG_API_BASE ?? "http://127.0.0.1:8020";

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

export function apiUrl(
  path: string,
  query?: Record<string, string | number | undefined | null>,
  base: string = API_BASE,
): string {
  const url = new URL(path.startsWith("/") ? path : `/${path}`, base);
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
    const body = (await res.json()) as Partial<ErrorEnvelope> & {
      detail?: unknown;
      layer?: string;
      hits?: string[];
      context?: Record<string, unknown>;
    };
    if (body.error) {
      kind = body.error.kind ?? kind;
      message = body.error.message ?? message;
      details = body.error.details ?? {};
    } else if (typeof body.detail === "string") {
      // 配置服务的错误体：{detail: "人话", context?} —— 技能扫描未通过时还带 layer/hits
      kind = body.layer ? "scan_blocked" : `http_${res.status}`;
      message = body.detail;
      details = { ...(body.context ?? {}), ...(body.layer ? { layer: body.layer, hits: body.hits ?? [] } : {}) };
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
  /** 默认 API_BASE（运行时）；配置服务传 CONFIG_API_BASE */
  base?: string;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = "GET", query, body, headers = {}, signal, base } = opts;
  // ★ FormData（上传技能包）不能手设 Content-Type：边界串要由浏览器生成
  const isForm = typeof FormData !== "undefined" && body instanceof FormData;

  const res = await fetch(apiUrl(path, query, base), {
    method,
    signal,
    headers: {
      ...(body !== undefined && !isForm ? { "Content-Type": "application/json" } : {}),
      ...authHeaders(),
      ...headers,
    },
    body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
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

/** 配置服务（atlas-config）的请求出口。身份头、错误解包与运行时同一套。 */
export const configHttp = {
  get: <T>(path: string, query?: Query) => request<T>(path, { query, base: CONFIG_API_BASE }),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body, base: CONFIG_API_BASE }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body, base: CONFIG_API_BASE }),
  /** 原始文本（在线查看技能包里的文件） */
  text: async (path: string): Promise<string> => {
    const res = await fetch(apiUrl(path, undefined, CONFIG_API_BASE), { headers: authHeaders() });
    if (!res.ok) throw await toApiError(res);
    return res.text();
  },
};
