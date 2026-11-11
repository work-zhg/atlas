# @atlas/jobs-ui

定时任务库 `atlas_jobs`（`infra-platform/libs/jobs`）的管理界面 —— **任何挂了 `JobRoutes` 的应用直接嵌入**。
原型：`c4-model/infra-platform/ui-jobs.html`。

## 这一版的范围

只读、只有**实例任务**（原型里的「系统机制」）：定义写在代码里，每个实例各跑一份，不写执行记录。
列表（名称、触发规则、运行份数、状态）→ 详情抽屉（本实例的运行情况、连续失败与最近错误），每 5 秒自动刷新。
原型里的新建、编辑、暂停、立即触发、执行历史、分片进度，要等库实现集群任务后再加。

★ 指标在进程内存里，接口返回的是**回应请求的那个实例**的情况，面板顶部写明实例标识；多副本时每次刷新可能换实例。

## 内容

| 文件 | 说明 |
| --- | --- |
| `JobsPanel.tsx` | 面板：本实例提示、搜索、列表、自动刷新 |
| `JobDetailDrawer.tsx` | 详情抽屉 |
| `JobStatus.tsx` | 名称（🔒 系统机制）与状态（启用 / 运行中 + 连续失败） |
| `format.ts` | 周期、耗时、时刻、相对时间；每秒走一次的「现在」 |
| `api.ts` | 接口类型、请求与 hook（与 `routes.py` 一一对应） |

## 用法

```tsx
import { JobsPanel } from "@atlas/jobs-ui";

<JobsPanel endpoint={`${API_BASE}/v1/jobs`} headers={authHeaders} />
```

- 宿主要有 antd 5 与 @tanstack/react-query 5（`QueryClientProvider`）；样式只用 antd 主题变量，页面标题、导航由宿主画。
- 后端：`app.include_router(JobRoutes(jobs, guard, instance=...).router)`；任务类写 `description` 才有中文名。
- 现在的宿主：

  | 页面 | 服务 · 接口 | 权限 | 任务 |
  | --- | --- | --- | --- |
  | Atlas `/jobs` · Atlas 服务 | atlas-server `/v1/jobs` | `job:manage` | 兜底、续租 |
  | Atlas `/jobs` · 知识库 | atlas-knowledge `/v1/jobs` | `job:manage` | 入库巡检 |
  | Atlas `/jobs` · MCP 网关 | atlas-mcpgateway `/api/v1/jobs` | `mcp:manage` | 刷新工具定义 |
  | Atlas `/jobs` · 模型平台 | atlas-modelgateway `/api/v1/jobs` | `model:manage` | LiteLLM 对账 |
  | TeamFlow 系统设置 › 定时任务 | atlas-teamflow `/api/v1/jobs` | `settings:manage` | 协作者对账、扫描待处理节点 |

  Atlas 的页面按服务分页签、按权限显隐，只挂当前页签的面板（不在看的不刷新）。

## 约定

- 错误体认三种：`{detail}`（FastAPI、知识库、两个网关）、`{code, message}`（TeamFlow）、`{error: {message}}`（Atlas）。
- 以源码形式被消费：消费方 `next.config` 的 `transpilePackages` 含 `@atlas/jobs-ui`；前端镜像
  （`docker/frontends/Dockerfile`）安装依赖前要拷它的 `package.json`。
- 接口类型手写：改 `routes.py` 的 `JobsOut` / `InstanceJobOut` 时同步改 `api.ts`。
- 改完跑：`cd packages/jobs-ui && pnpm typecheck`、`pnpm --filter atlas-web check`。
