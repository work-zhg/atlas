/**
 * HTTP 封装 —— 所有 REST 请求的唯一出口。
 *
 * 两件事集中在这里，别在别处重复：
 *   1. 请求头（authHeaders）与登录态。访问 JWT 在 HttpOnly Cookie 里，API 网关本地验签后写身份头；
 *      前端只带 CSRF 双提交头，从不自己写 X-Uid。接口回 401 先单飞刷新再重放（@atlas/chat-ui 的
 *      lib/session.ts，c4-model/apigateway/dynamic-web-jwt.html）。
 *   2. Atlas 自己的接口端点（API_BASE / 技能应用等）。错误信封解包、URL 拼接、请求本身在
 *      共享包 @atlas/chat-ui 的 lib/http.ts（对话组件与 AI TeamFlow 共用同一份）。
 */

import {
  apiUrl as coreApiUrl,
  makeHttp,
  request as coreRequest,
  toApiError,
  type AtlasClientConfig,
  type RequestOptions as CoreRequestOptions,
} from "@atlas/chat-ui/src/lib/http";
import { csrfHeaders, refreshSession } from "@atlas/chat-ui/src/lib/session";

export { ApiError, toApiError, type ErrorEnvelope, type Query } from "@atlas/chat-ui/src/lib/http";

/** 部署子路径（与 next.config 的 basePath 一致）。原生 <a href> / window.open 不会自动加，要手动拼。 */
export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";
/**
 * 技能应用（atlas-skill：技能的上传、审查、发布、下架；用户中心里的独立应用）。浏览器直连它
 * （权限设计 §9）；部署时经 API 网关走同源路径（/skill），登录 Cookie 才会带上。
 */
export const SKILL_API_BASE =
  process.env.NEXT_PUBLIC_SKILL_API_BASE ?? "http://127.0.0.1:8020";
/** 知识库服务（atlas-knowledge，doc/atlas-rag-design.html）。同样浏览器直连、经网关同源。 */
export const KNOWLEDGE_API_BASE =
  process.env.NEXT_PUBLIC_KNOWLEDGE_API_BASE ?? "http://127.0.0.1:8050";
/**
 * MCP 网关（atlas-mcpgateway，doc/mcp-design.html 第二版）的管理端接口：服务、复核、API Key、
 * 授权、访问日志。同样浏览器直连、经网关同源（/mcp）。
 */
export const MCP_GATEWAY_API_BASE =
  process.env.NEXT_PUBLIC_MCP_GATEWAY_API_BASE ?? "http://127.0.0.1:8070";
/**
 * 模型管理平台（atlas-modelgateway，agent-platform/doc/design/model-design.html）的管理端接口：
 * 模型、授权、API Key。同样浏览器直连、经网关同源（/model）。
 */
export const MODEL_GATEWAY_API_BASE =
  process.env.NEXT_PUBLIC_MODEL_GATEWAY_API_BASE ?? "http://127.0.0.1:8090";

/**
 * 请求头：身份在 HttpOnly Cookie 里的访问 JWT（同源请求自动带上），API 网关验签后写成 X-Uid ——
 * 前端从不自己写身份。写请求带 CSRF 双提交头（值取自可读的 uc_csrf Cookie，网关校验）。
 * SSE 也要用这份 header，故导出（这正是不用原生 EventSource 的原因）。
 */
export function authHeaders(): Record<string, string> {
  return csrfHeaders();
}

/** Atlas 前端自己的接口端点：注入给对话组件（AtlasClientProvider）与下面的 http。 */
const endpoint = (apiBase: string): AtlasClientConfig => ({
  apiBase,
  headers: authHeaders,
  refresh: refreshSession,
});
export const atlasClient = endpoint(API_BASE);
const skillClient = endpoint(SKILL_API_BASE);
const knowledgeClient = endpoint(KNOWLEDGE_API_BASE);
const mcpGatewayClient = endpoint(MCP_GATEWAY_API_BASE);
const modelGatewayClient = endpoint(MODEL_GATEWAY_API_BASE);

export function apiUrl(
  path: string,
  query?: Record<string, string | number | undefined | null>,
  base: string = API_BASE,
): string {
  return coreApiUrl(path, query, base);
}

export interface RequestOptions extends CoreRequestOptions {
  /** 默认 API_BASE（运行时）；其它服务传各自的 *_API_BASE */
  base?: string;
}

export function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { base, ...rest } = opts;
  return coreRequest<T>(base ? endpoint(base) : atlasClient, path, rest);
}

export const http = makeHttp(atlasClient);

/** 技能应用（atlas-skill）的请求出口。身份头、错误解包与运行时同一套。 */
export const skillHttp = {
  ...makeHttp(skillClient),
  /** 原始文本（在线查看技能包里的文件） */
  text: async (path: string): Promise<string> => {
    const send = () => fetch(apiUrl(path, undefined, SKILL_API_BASE), { headers: authHeaders() });
    let res = await send();
    if (res.status === 401 && (await refreshSession())) res = await send();
    if (!res.ok) throw await toApiError(res);
    return res.text();
  },
};

/** 知识库服务（atlas-knowledge）的请求出口。 */
export const knowledgeHttp = makeHttp(knowledgeClient);

/** MCP 网关管理端接口的请求出口。 */
export const mcpGatewayHttp = makeHttp(mcpGatewayClient);

/** 模型网关管理端接口的请求出口。 */
export const modelGatewayHttp = makeHttp(modelGatewayClient);
