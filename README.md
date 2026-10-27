# Atlas

一个自建的多智能体对话平台：在 Web 工作台里配置智能体（模型、提示词、工具、子智能体、运行限制），
然后与它多轮对话 —— 并把这一轮里**模型说了什么、调了哪些工具、委派了哪些子智能体、写了哪些文件**，
作为一条可回放的事件流实时呈现出来。

左侧是会话列表，中间是对话流与执行计划，右侧 Inspector 分「计划 / 工具 / 文件」三页实时展开
这一轮的全过程，底部是本轮 token 消耗。运行中可随时停止 —— 取消是协作式的，不会切断
进行中的工具调用。

> 📊 **[交互式架构图](agent-platform/doc/atlas-overview.html)** —— 下载后用浏览器打开，支持明暗主题切换，
> 可单独点亮「请求主链路」「事件回传通道」「会话 Pod 执行路径」三条链路。

---

## 架构

```mermaid
flowchart LR
  web["web · Next.js 15<br/>会话工作台 · 事件归约"]

  subgraph server["server · atlas_agent（FastAPI）"]
    direction LR
    api["api/v1<br/>agents · threads · runs"]
    services["services/<br/>幂等 · 会话串行锁"]
    executor["executor/<br/>后台 asyncio.Task"]
    runtime["AgentRuntime<br/>「一轮怎么跑」的接缝"]
    api --> services --> executor --> runtime
  end

  engine["engine · atlas_engine<br/>kernel（图+中间件）· contracts"]
  bridge["bridge<br/>Pod 内 1 号进程"]
  osb["OpenSandbox<br/>lifecycle server + 控制器"]
  gw["Higress AI 网关<br/>模型调用 · MCP 代理"]
  oss[("对象存储 · S3<br/>会话工作区")]
  pg[("PostgreSQL 16")]
  redis[("Redis 7<br/>Stream · 锁 · 取消位")]

  web -->|"HTTP · SSE"| api
  runtime -->|"native：装配 kernel 图"| engine
  runtime -->|"ACP over WS"| bridge
  runtime -->|"ensure Pod（幂等）"| osb
  osb -->|"建 Pod"| bridge
  engine -->|"模型调用（stream）"| gw
  engine -.->|"FilesystemProtocol"| oss
  bridge -->|"挂载 /workspace"| oss
  services -->|"repositories/ 唯一入口"| pg
  executor -->|"XADD run:events"| redis
  redis -.->|"XREAD → SSE 推流"| api
```

三层，依赖严格单向：

| 层 | 职责 | 关键约定 |
|---|---|---|
| `web` | 会话工作台 · 智能体编辑器 · 事件归约与渲染 | 只认事件契约与 REST 契约，不感知内核 |
| `server` | 路由 / 编排 / 装配 / 执行循环 / 持久化 / 凭据注入 | 唯一接触数据库的地方在 repository 层；`domain/` 与 `executor/{build,runner}` 是其中的**纯计算层**，禁碰基础设施 |
| `engine` | `kernel`（图 + 全部中间件）+ `contracts`（能力协议与错误分类学） | **纯库**：无 Web 框架、无 DB session、无 HTTP、无凭据 |

依赖方向不靠自觉，靠 CI —— import-linter 契约规定 `engine` 里出现 Web 框架或数据库的
import 就红；`server` 的纯计算层同样有 contract 拦着，「run 一轮」始终能用假模型脱离
数据库单测。`acp` 与 `bridge` 另有独立契约：前者要能脱离整个依赖树装进 Pod 镜像，
后者不许认识 `server`（Pod 在跑时无法原地升级）。

### 一轮 run 的骨架

- `POST /threads/{id}/runs` 立刻返回 `202 {run_id}`，不等执行
- `executor` 在后台 `asyncio.Task` 里跑完整轮
- **终态必须先落库，再发布 `run.finished`** —— 顺序反了会让前端看到「结束了但读不到结果」

### AgentRuntime 的两个实现

「一轮怎么跑」被收敛成一个接缝，两个实现产出同样的 `AsyncIterator[TraceEvent]`，
落库、串行锁、取消与审批都在外层共用：

| 实现 | 怎么跑 | 用于 |
|---|---|---|
| `NativeRuntime` | 本进程装配 kernel 图，直连模型网关 | 平台自己的智能体 |
| `AcpRuntime` | ACP over WebSocket 接会话 Pod 内的 `bridge` | 把外部 CLI 智能体接成平台的一种后端 |

---

## 核心能力

### 过程可见、可回放

待办清单、每次工具调用的入参与结果、子智能体的委派边界、文件产物、token 消耗，
按严格有序的事件流实时推送。刷新页面、切后台、开多个标签页都能从断点续上 ——
**游标是事件序号，断线重连自动补发缺口**。

事件经 Redis Stream 出站，这同时解决了两个问题：多 worker 部署下 `POST` 与 `GET` 落在
不同进程，以及断线重连。超出 Stream TTL 的历史从 Postgres 的归档表回放。

### 智能体配置与版本

- 模型、提示词、工具、子智能体、运行限制均可配置
- **保存即产生新版本**：历史会话永远指向当时那份配置快照，可复现、可回滚
- 草稿态可直接试跑，不落会话、不产生版本
- 工具按需勾选；勾了却缺前提的会明确报出来，而不是静默失效

### 隔离执行 · 会话 Pod

ACP 会话对应 `atlas-acp` 命名空间里的一个 Pod，原生智能体的 Shell 沙箱在 `atlas-sbx`。
两种 Pod 都由开源的 [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) 创建
（`agent-platform/doc/sandbox-opensandbox-design.html`）：复用、配额、git 档案授权、回收这些按会话的判断在
`server`，**Atlas 的服务都不持有 K8s 权限**。

工作区是挂进 Pod 的 `/workspace`，背后是对象存储（整桶的写穿式 S3 CSI 卷，按会话 subPath 挂载）；
对象存储凭据只在 CSI 驱动里，git 凭据经 Credential Vault 注入，Pod 里都看不到。

### 子智能体委派

主智能体把子任务整体交给另一个智能体，在**独立上下文**里执行，只拿回结论 ——
子任务的中间过程不会污染主对话。委派深度结构性封顶为 1。

**子智能体持有自己的会话。** 第二次委派给同一个子智能体时恢复上次的上下文，
不用把背景重抄一遍；它有自己的技能、自己的压缩边界，工作区与主智能体共享。
一次委派就是子会话上的一个 run —— 于是它有独立的事件流、独立的审批、独立的取消。

两点由此而来的约束：

- **并行只能跨不同的子智能体**。同名委派会撞上会话串行锁，工具层直接拒绝并给出
  替代方案，而不是让模型等到超时。需要真并行时把子智能体配成 `session_mode: ephemeral`。
- **重试会看到上次的痕迹**。模型可以用 `fresh=true` 主动要一个干净的开始
  （旧会话归档而非删除）。

### 人工确认（HITL）

高风险工具执行前挂起等人点头，弹窗展示完整参数。
**拒绝不终止本轮** —— 作为工具结果回给智能体，它可以换个方案继续。
Shell 默认强制进入审批，这条写在 schema 层，绕过前端直接调 API 同样被拦。

### 上下文压缩与跨会话记忆

接近上下文窗口时自动摘要，并以事件明确告诉用户「这里压缩过」——
压缩只改变模型视角，原始消息一条不丢。跨会话记忆走 Mem0，Qdrant 是它的向量后端。

### 技能与 MCP

技能按 `(slug, version)` 从注册表投送，不扫描文件系统。MCP 工具经网关代理接入，
与内置工具走同一套门禁与审批。

### 可观测性

`server` 只往 OTel Collector 发 OTLP，不直连任何后端 —— Collector 是唯一收口，
换后端只改一份配置。**Jaeger 看 APM 维度**（冷启动、NAS 抖动、WS 断连），
**Langfuse 看 LLM 维度**，两者分流不是二选一。

---

## 部署形态

```mermaid
flowchart LR
  browser["web :3000"]

  subgraph sys["K8s · atlas-system"]
    server["atlas-server<br/>FastAPI :8000"]
  end

  osb["K8s · opensandbox-system<br/>lifecycle server ×2 + 控制器"]

  subgraph sess["K8s · atlas-acp（每会话一个）"]
    bridge["bridge<br/>WS 服务端"]
    cli["CLI adapter<br/>+ 真 CLI（黑盒）"]
  end

  subgraph obs["可观测性 —— Collector 唯一收口"]
    otel["OTel Collector<br/>:4318 / :4317"]
    jaeger["Jaeger :16686"]
    langfuse["Langfuse :3001"]
  end

  subgraph data["有状态依赖"]
    pg[("PostgreSQL :5433")]
    redis[("Redis :6380")]
    qdrant[("Qdrant :6333")]
    minio[("MinIO S3 :9000")]
  end

  gw["Higress AI 网关"]

  browser -->|"HTTP · SSE"| server
  server -->|"ensure / delete / reap"| osb
  osb -->|"建 Pod"| bridge
  server -->|"ACP over WS（per-Pod token）"| bridge
  bridge -->|"ACP over stdio"| cli
  cli -->|"模型调用"| gw
  server -.->|"OTLP"| otel
  otel --> jaeger
  otel -.-> langfuse
  server --> pg & redis & qdrant & minio
  bridge -->|"挂载 /workspace"| minio
```

**ACP 是两段传输**：`server ↔ bridge` 走 WebSocket（不经过 OpenSandbox），
`bridge ↔ CLI adapter` 走 stdio 上的行分隔 JSON。两端共用同一份帧模型（`atlas_acp`），
理解漂移会在类型层当场暴露，而不是到线上才发现。

---

## 目录结构

仓库里有三个**平级的产品**：Agent 平台（Atlas）、用户中心、AI TeamFlow，外加它们共用的基础设施平台。
顶层按产品分，产品内部再按部署形态分。服务目录与包同名（`services/agent` 即 `atlas_agent`）——
一个产品以后可以有多个服务，目录名要说明是哪一个，而不是统一叫 server。

```
agent-platform/            Agent 平台（Atlas）
├── services/              平台后端服务（独立部署，K8s）
│   ├── agent/             API + 执行器（atlas_agent）
│   ├── config/            技能 / MCP / 持久指令（atlas_config）
│   └── knowledge/         知识库与检索（atlas_knowledge）
├── libs/                  纯库，不单独部署
│   ├── engine/            智能体内核（atlas_engine）
│   ├── host/              server ⇄ bridge 上游协议（atlas_host）
│   ├── acp/               ACP 协议（atlas_acp）
│   └── jsonrpc/           JSON-RPC 2.0（atlas_jsonrpc）
├── pod/                   跑在会话 Pod 里的进程
│   └── bridge/            ACP bridge（atlas_bridge）
├── web/                   控制台前端
├── bench/                 评测（RAG + Agent）
└── tests/                 平台的测试（agent / engine / bridge）
infra-platform/            基础设施平台（跨产品共用）
└── libs/
    └── redislock/         Redis 分布式锁（atlas_redislock）
usercenter/                用户中心
├── services/usercenter/   后端（atlas_usercenter）：pyproject.toml src/ tests/
└── web/                   前端
teamflow/                  AI TeamFlow
├── services/teamflow/     后端 API 与 worker（atlas_teamflow）：pyproject.toml src/ tests/
└── web/                   前端
packages/chat-ui/          跨产品共享的对话组件（控制台与 TeamFlow 共用）
deploy/  docker/           部署清单与镜像（跨产品）
doc/  prototype/           设计文档与原型
```

## 技术栈

Python 3.13 · FastAPI · SQLAlchemy (async) · PostgreSQL 16 · Redis 7 · Qdrant ·
MinIO/S3 · Kubernetes · OpenTelemetry · Next.js 15 · Ant Design 5 · LangGraph

---

## 快速开始

```bash
# 1. 依赖服务（PostgreSQL :5433 / Redis :6380，非标端口避免与本机冲突；
#    MinIO :9000 提供 S3 会话工作区，控制台 :9001，桶自动建好；
#    OTel Collector :4318 + Jaeger UI :16686，遥测默认关，见 .env.example；
#    Qdrant :6333 承载跨会话记忆，记忆默认关，见 .env.example）
#
# 可选：Langfuse（LLM 维度的可观测性，与 Jaeger 分流不是二选一）
#   OTEL_COLLECTOR_CONFIG=collector-langfuse.yaml \
#     docker compose --profile langfuse up -d      # UI http://localhost:3001
make up

# 2. Python 依赖与数据库迁移
make sync
make migrate

# 3. 前端依赖
make web-install

# 4. 配置环境变量
cp .env.example .env       # 填入模型网关地址与 key
cp agent-platform/web/.env.local.example agent-platform/web/.env.local

# 5. 前后端一起起（后端 :8000，前端 :3000）
make dev
```

浏览器打开 http://127.0.0.1:3000

### 常用命令

```bash
make check        # lint + 依赖契约 + 测试
make test         # 测试
make arch         # 校验分层依赖方向
make web-check    # 前端类型检查
make web-gen      # 从运行中的后端重新生成 API 类型（结果需提交）
```

> ⚠️ 测试会清空所配置数据库中的非内置数据。请用独立的测试库：
> ```bash
> DATABASE_URL='postgresql+asyncpg://atlas:atlas@localhost:5433/atlas_test' uv run pytest
> ```

### 本机的 OpenSandbox

会话 Pod 由 OpenSandbox 创建。本机（Docker Desktop K8s）装 CSI 驱动、控制器与两个 lifecycle server：
`deploy/local/opensandbox.sh`；对真 OpenSandbox 的实测：`deploy/local/opensandbox_probe.sh`。
单测用进程内的假 OpenSandbox（`agent-platform/tests/opensandbox_fake.py`），不需要集群。

### 部署到服务器

单节点 K8s（默认 192.168.0.112），全套一起：依赖服务（PG / Redis / MinIO / Qdrant / OTel + Jaeger / Langfuse /
Higress）放在 `atlas-infra`，只开 ClusterIP；对外只有 traefik 上的 `atlas.<IP>.nip.io` 与 `langfuse.<IP>.nip.io`。

```bash
deploy/server/images.sh            # 自建镜像构建 amd64 并导入节点；third = 第三方镜像
deploy/server/deploy.sh            # 依赖服务 → OpenSandbox → server 迁移 → 后端 → 前端 → Ingress → MCP → 首个管理员
deploy/server/register.sh          # 首次：admin 改过密码后，把 Atlas、TeamFlow 登记到用户中心
```

应用清单与本机共用 `deploy/local/*.yaml`，差别只在依赖服务的地址（`apply.sh` 的 `render`）。

---

## 实现进度

上面描述的是**目标形态**。当前各能力的落地状态：

| 能力 | 状态 | 说明 |
|---|---|---|
| 事件流 · 断线续传 · 回放 | ✅ 可用 | Redis Stream + Postgres 归档 |
| 智能体配置与版本快照 | ✅ 可用 | |
| 子智能体委派（持有会话） | ✅ 可用 | 深度封顶 1，串行锁生效 |
| 人工确认（HITL） | ✅ 可用 | schema 层强制，API 直调同样拦 |
| 上下文压缩 | ✅ 可用 | |
| ACP 执行形态（`bridge` / OpenSandbox） | ✅ 可用 | 会话 Pod 由 OpenSandbox 创建；集群内实测见 `deploy/local/opensandbox_probe.sh` |
| 跨会话记忆（Mem0 + Qdrant） | ✅ 可用 | 默认关，见 `.env.example` |
| 可观测性（OTel / Jaeger / Langfuse） | ✅ 可用 | 默认关 |
| 技能投送 · MCP 接入 | ✅ 可用 | MCP 经 Higress 收口（本机：`docker compose --profile higress up -d higress` + `deploy/local/higress/setup.py`）；网页搜索即 Higress 上的 serpapi MCP，不再是内置工具 |
| **native 形态的隔离执行** | 🚧 **接入中** | `SandboxProtocol` 与 `SandboxMiddleware` 的接缝已在位，但**尚无实现方** —— K8s Pod 执行环境待接入。期间 `bash` 在工具目录里标为不可用，勾了会明确报出来而不是静默失效 |
| 模型网关切到 Higress | 📋 规划 | MCP 已经过 Higress；模型调用仍直连 LiteLLM 网关，切换后两者收口到同一处 |
| 鉴权 / 多租户 | 📋 规划 | 当前身份注入收敛在 `identity.py` 单点，换成 JWT / SSO 只改这一个函数 |

---

## 文档

设计文档在 [`agent-platform/doc/`](agent-platform/doc/README.md)，浏览器直接打开 HTML 即可，不需要起服务。
顶层一个系统一页，讲清楚它解决什么问题、边界在哪、哪些做法被否决过以及为什么；
[`agent-platform/doc/detail/`](agent-platform/doc/detail/index.html) 是几处需要展开推导的深水区。

**这些文档会被修正** —— 设计先写、实现后跑，跑通之后发现设计错了的地方会回来改文档，
并且写明原来错在哪。

---

## 许可

[MIT](./LICENSE)
