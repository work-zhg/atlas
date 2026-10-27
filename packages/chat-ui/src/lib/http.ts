/**
 * Atlas 接口的请求核心 —— 与部署无关：接口根地址与身份头由调用方注入（AtlasClientConfig）。
 *
 *   · Atlas 前端注入自己的 API_BASE 与请求头（web/src/lib/http.ts）；
 *   · 嵌入方（如 AI TeamFlow）注入它的代理地址与自己的会话 / CSRF 头。
 *
 * 错误信封解包在这里集中：后端统一返回 {"error": {kind, message, details}}，
 * 前端一律转成 ApiError，组件按 kind 分支而不是猜 HTTP 码。
 */

/** 一个 Atlas 接口端点：根地址（可带同源路径前缀，如 /atlas-api）+ 每次请求附带的头。 */
export interface AtlasClientConfig {
  apiBase: string;
  headers?: () => Record<string, string>;
}

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

export function apiUrl(
  path: string,
  query: Record<string, string | number | undefined | null> | undefined,
  base: string,
): string {
  // ★ 不能写 new URL(path, base)：path 以 / 开头时会丢掉 base 的路径部分，
  //   而部署在网关 / 代理后面时 base 是同源子路径（如 /atlas-api）。先拼字符串，再相对当前页面解析。
  const joined = `${base.replace(/\/+$/, "")}${path.startsWith("/") ? path : `/${path}`}`;
  const origin = typeof window === "undefined" ? "http://localhost" : window.location.origin;
  const url = new URL(joined, origin);
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }
  return url.toString();
}

export async function toApiError(res: Response): Promise<ApiError> {
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
}

export async function request<T>(client: AtlasClientConfig, path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = "GET", query, body, headers = {}, signal } = opts;
  // ★ FormData（上传技能包）不能手设 Content-Type：边界串要由浏览器生成
  const isForm = typeof FormData !== "undefined" && body instanceof FormData;

  const res = await fetch(apiUrl(path, query, client.apiBase), {
    method,
    signal,
    credentials: "same-origin",
    headers: {
      ...(body !== undefined && !isForm ? { "Content-Type": "application/json" } : {}),
      ...(client.headers?.() ?? {}),
      ...headers,
    },
    body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
  });

  if (!res.ok) throw await toApiError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export interface Http {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) => Promise<T>;
  post: <T>(path: string, body?: unknown, headers?: Record<string, string>) => Promise<T>;
  patch: <T>(path: string, body?: unknown) => Promise<T>;
  put: <T>(path: string, body?: unknown) => Promise<T>;
  del: <T>(path: string) => Promise<T>;
}

export function makeHttp(client: AtlasClientConfig): Http {
  return {
    get: <T,>(path: string, query?: Query, signal?: AbortSignal) => request<T>(client, path, { query, signal }),
    post: <T,>(path: string, body?: unknown, headers?: Record<string, string>) =>
      request<T>(client, path, { method: "POST", body, headers }),
    patch: <T,>(path: string, body?: unknown) => request<T>(client, path, { method: "PATCH", body }),
    put: <T,>(path: string, body?: unknown) => request<T>(client, path, { method: "PUT", body }),
    del: <T,>(path: string) => request<T>(client, path, { method: "DELETE" }),
  };
}
