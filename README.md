# Atlas

一个自建的多智能体对话平台：在 Web 工作台里配置智能体（模型、提示词、工具、子智能体、运行限制），
然后与它多轮对话 —— 并把这一轮里**模型说了什么、调了哪些工具、委派了哪些子智能体、写了哪些文件**，
作为一条可回放的事件流实时呈现出来。

左侧是会话列表，中间是对话流与执行计划，右侧 Inspector 分「计划 / 工具 / 文件」三页实时展开
这一轮的全过程，底部是本轮 token 消耗。运行中可随时停止 —— 取消是协作式的，不会切断
进行中的工具调用。

> 📊 **[交互式架构图](./doc/atlas-overview.html)** —— 下载后用浏览器打开，支持明暗主题切换，
> 可单独点亮「请求主链路」「事件回传通道」「会话 Pod 执行路径」三条链路。

---

## 架构

```mermaid
flowchart LR
  web["web · Next.js 15<br/>会话工作台 · 事件归约"]

  subgraph server["server · atlas_server（FastAPI）"]
    direction LR
    api["api/v1<br/>agents · threads · runs"]
    services["services/<br/>幂等 · 会话串行锁"]
    executor["executor/<br/>后台 asyncio.Task"]
    runtime["AgentRuntime<br/>「一轮怎么跑」的接缝"]
    api --> services --> executor --> runtime
  end

  engine["engine · atlas_engine<br/>kernel（图+中间件）· contracts"]
  bridge["bridge<br/>Pod 内 1 号进程"]
  cluster["cluster 服务<br/>唯一持 K8s 写权限"]
  gw["Higress AI 网关<br/>模型调用 · MCP 代理"]
  oss[("对象存储 · S3<br/>会话工作区")]
  pg[("PostgreSQL 16")]
  redis[("Redis 7<br/>Stream · 锁 · 取消位")]

  web -->|"HTTP · SSE"| api
  runtime -->|"native：装配 kernel 图"| engine
  runtime -->|"ACP over WS"| bridge
  runtime -->|"ensure Pod（幂等）"| cluster
  cluster -->|"建 Pod · 配对 Secret"| bridge
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

每个会话对应 `atlas-sessions` 命名空间里的一个 Pod，闲置回收。Pod 由 `cluster` 服务
幂等供给 —— **它是全仓唯一持有 K8s 写权限的组件**，`server` 只认它的 API 契约、
不碰内部实现，这样「Pod 生命周期归谁」只有一个答案。

工作区是挂进 Pod 的 `/workspace`，背后是对象存储；模型凭据经 Secret 注入，随 Pod 生灭。
`server` 与 `cluster` 同在 `atlas-system`，会话 Pod 起在 `atlas-sessions` ——
两个 namespace 是为了让「唯一写权限」在 RBAC 上真正成立，而不只是约定。

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
    clu["atlas-cluster<br/>FastAPI :8010"]
  end

  subgraph sess["K8s · atlas-sessions（每会话一个，闲置回收）"]
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
  server -->|"ensure / delete / reap"| clu
  clu -->|"建 Pod · 配对 Secret"| bridge
  server -->|"ACP over WS（per-Pod token）"| bridge
  bridge -->|"ACP over stdio"| cli
  cli -->|"模型调用"| gw
  server -.->|"OTLP"| otel
  otel --> jaeger
  otel -.-> langfuse
  server --> pg & redis & qdrant & minio
  bridge -->|"挂载 /workspace"| minio
```

**ACP 是两段传输**：`server ↔ bridge` 走 WebSocket（不经过 `cluster`），
`bridge ↔ CLI adapter` 走 stdio 上的行分隔 JSON。两端共用同一份帧模型（`atlas_acp`），
理解漂移会在类型层当场暴露，而不是到线上才发现。

---

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
cp web/.env.local.example web/.env.local

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

### 本机跑一个最小 K8s 集群

`cluster` 服务的 `KubernetesBackend` 有一组**对着真集群**的测试
（`tests/test_cluster_k8s.py`），没有集群时整体跳过。本机验证：

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server \
    --disable=traefik --disable=servicelb --disable=metrics-server \
    --disable=local-storage --write-kubeconfig-mode=644" sh -
pip install kubernetes-asyncio          # cluster 的可选依赖
pytest tests/test_cluster_k8s.py -q     # 自动发现 /etc/rancher/k3s/k3s.yaml
```

约 1.5GB 内存即可（k3s 裁掉了 traefik / servicelb / metrics-server）。
卸载：`/usr/local/bin/k3s-uninstall.sh`。

**为什么这组测试不可省**：内存后端覆盖主体逻辑（模板、配额、幂等、孤儿判定），
但它有一处刻意不对齐 —— delete 立即生效，而 K8s 是异步的优雅终止。
第一次对着真集群冒烟就抓到三个被它掩盖的 bug。

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
| ACP 执行形态（`bridge` / `cluster`） | ✅ 可用 | 含对真 k3s 集群的测试 |
| 跨会话记忆（Mem0 + Qdrant） | ✅ 可用 | 默认关，见 `.env.example` |
| 可观测性（OTel / Jaeger / Langfuse） | ✅ 可用 | 默认关 |
| 技能投送 · MCP 接入 | ✅ 可用 | |
| **native 形态的隔离执行** | 🚧 **接入中** | `SandboxProtocol` 与 `SandboxMiddleware` 的接缝已在位，但**尚无实现方** —— K8s Pod 执行环境待接入。期间 `bash` 在工具目录里标为不可用，勾了会明确报出来而不是静默失效 |
| 模型网关切到 Higress | 📋 规划 | 当前直连 LiteLLM 网关；切换后 MCP 工具代理与模型调用收口到同一处 |
| 鉴权 / 多租户 | 📋 规划 | 当前身份注入收敛在 `identity.py` 单点，换成 JWT / SSO 只改这一个函数 |

---

## 文档

设计文档在 [`doc/`](./doc/README.md)，浏览器直接打开 HTML 即可，不需要起服务。
顶层一个系统一页，讲清楚它解决什么问题、边界在哪、哪些做法被否决过以及为什么；
[`doc/detail/`](./doc/detail/index.html) 是几处需要展开推导的深水区。

**这些文档会被修正** —— 设计先写、实现后跑，跑通之后发现设计错了的地方会回来改文档，
并且写明原来错在哪。

---

## 许可

[MIT](./LICENSE)
