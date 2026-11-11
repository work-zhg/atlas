/**
 * 定时任务的管理接口 —— 与 infra-platform/libs/jobs/src/atlas_jobs/routes.py 一一对应：
 *
 *   GET   {endpoint}      本进程的实例任务：周期、正在不在跑、下一轮、运行与失败次数、最近一轮
 *
 * endpoint = 宿主挂 JobRoutes 的位置（如 Atlas 是 `${API_BASE}/v1/jobs`）。
 * ★ 实例任务每个进程各跑一份、指标在进程内存里：返回的是回应请求的那个实例的情况（instance）。
 * ★ 类型手写、跟着 routes.py 的 JobsOut / InstanceJobOut 改。
 */
import { useQuery } from "@tanstack/react-query";

export interface InstanceJobOut {
  type: string;
  /** 给人看的名字；为空就显示 type */
  description: string;
  /** instance = 每个实例各跑一份（集群任务实现后会有别的取值） */
  scope: "instance";
  /** 两轮之间隔多久（秒） */
  every: number;
  /** 每轮在 every 上随机加减的秒数 */
  jitter: number;
  running: boolean;
  /** 下一轮大约什么时候开始；正在跑时为空 */
  next_fire_at: string | null;
  runs: number;
  failures: number;
  /** 连续失败次数，成功一次就清零 */
  consecutive_failures: number;
  last_started_at: string | null;
  last_finished_at: string | null;
  last_duration_s: number | null;
  last_error: string | null;
}

export interface JobsOut {
  /** 回应这个请求的实例：指标只是它的 */
  instance: string;
  jobs: InstanceJobOut[];
}

/** 接口在哪、每次请求带什么头。身份由宿主的会话 Cookie / 网关负责，组件不读环境变量。 */
export interface JobsClient {
  endpoint: string;
  headers?: () => Record<string, string>;
}

export class JobsError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "JobsError";
    this.status = status;
  }
}

async function toError(res: Response): Promise<JobsError> {
  let message = `${res.status} ${res.statusText}`;
  try {
    const body = (await res.json()) as { detail?: unknown; message?: unknown; error?: { message?: unknown } };
    // 各宿主的错误体：FastAPI / 知识库 / 网关 {detail}；TeamFlow {code, message}；Atlas {error: {message}}
    if (typeof body.detail === "string") message = body.detail;
    else if (typeof body.message === "string") message = body.message;
    else if (typeof body.error?.message === "string") message = body.error.message;
  } catch {
    // 响应不是 JSON（如 502 网关页），保留状态码即可
  }
  return new JobsError(res.status, message);
}

export async function listJobs(client: JobsClient): Promise<JobsOut> {
  const res = await fetch(client.endpoint.replace(/\/+$/, ""), {
    credentials: "same-origin",
    headers: client.headers?.() ?? {},
  });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as JobsOut;
}

/** 自动刷新的间隔；页面不在前台时 react-query 自己停 */
export const REFRESH_MS = 5000;

export function useJobs(client: JobsClient) {
  return useQuery({
    // 查询键带上 endpoint：同一页面嵌两个应用的任务也不会串缓存
    queryKey: ["atlas-jobs", client.endpoint],
    queryFn: () => listJobs(client),
    refetchInterval: REFRESH_MS,
    staleTime: 0,
    // 4xx 是业务结论（没权限），重试只会让人多等；不依赖宿主 QueryClient 的重试策略
    retry: (failureCount, error) => !(error instanceof JobsError && error.status < 500) && failureCount < 2,
  });
}
