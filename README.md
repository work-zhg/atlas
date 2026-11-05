# Atlas

企业内部的智能体平台。在控制台里配置智能体——模型、提示词、工具、技能、知识库、子智能体——然后与它多轮对话。
每一轮里模型说了什么、调了哪些工具、派了哪些子智能体、写了哪些文件，都作为一条有序的事件流实时推给前端；
刷新页面、断线重连都能从断点接着看。

智能体有两种跑法：

- **原生智能体**：在 Agent 服务的进程里跑（基于 LangGraph 的内核），Shell 命令在隔离的沙箱里执行。
- **CLI 智能体**：Claude Code 这类外部 CLI 跑在会话 Pod 里（每个会话一个），由 Pod 里的 bridge 接成平台的执行后端。

仓库里还有两个和它平级的产品：**用户中心**（统一登录、组织与权限）和 **AI TeamFlow**（让智能体作为团队成员参与研发流程）。

> **架构图**在 [`c4-model/`](c4-model/)：每个系统一套 C4 图，从系统上下文、容器、组件、按领域拆的代码图，到关键场景的时序图和 112 部署图。
> 都是单文件 HTML，浏览器直接打开。

---

## 系统组成

### Agent 平台（`agent-platform/`）

| 服务 | 目录 · 包 | 端口 | 做什么 |
|---|---|---|---|
| Agent 服务 | `services/agent` · `atlas_agent` | 8000 | 智能体、会话、run；原生和 CLI 两种运行时；运行事件经 Redis Stream 推成 SSE |
| 控制台 | `web`（Next.js） | 3000 | 会话工作台、智能体编辑器，以及技能、知识库、模型、MCP 的管理页 |
| 技能应用 | `services/skill` · `atlas_skill` | 8020 | 技能包的上传、检查、审查、发布、停用和下架 |
| 知识库服务 | `services/knowledge` · `atlas_knowledge` | 8050 | 文档入库（Celery worker）与混合检索，按库、按文档判权限 |
| 记忆服务 | `services/memory` · `atlas_memory` | 8060 | 按作用域（目前是「用户」）保存跨会话记忆，带修订历史 |
| MCP 网关 | `services/mcpgateway` · `atlas_mcpgateway` | 8070 | 所有 MCP 调用的唯一入口：代理 MCP Server，把 REST / gRPC 转成工具，按 API Key 授权 |
| 模型平台 | `services/modelgateway` · `atlas_modelgateway`，加 LiteLLM | 8090 / 4000 | LiteLLM 是模型网关；平台管模型登记、私有模型授权和 API Key，不在调用路径上 |
| bridge | `pod/bridge` · `atlas_bridge` | — | 会话 Pod 里的 1 号进程：一头连 Agent 服务（WebSocket），一头连 CLI（ACP over stdio） |
| 评测 | `bench` · `atlas_bench` | — | RAG 评测和 Agent 评测（含 Terminal-Bench 2），只经各服务的公开接口 |

库（不单独部署）：

| 库 | 目录 | 说明 |
|---|---|---|
| `atlas_engine` | `agent-platform/libs/engine` | 智能体内核（kernel：图 + 中间件）和能力协议（contracts）；纯库，不碰 Web 框架和数据库 |
| `atlas_host` | `agent-platform/libs/host` | Agent 服务 ⇄ bridge 的上游协议 |
| `atlas_acp` | `agent-platform/libs/acp` | ACP 协议的帧模型；要装进 Pod 镜像，不依赖本仓库任何其它包 |
| `atlas_jsonrpc` | `agent-platform/libs/jsonrpc` | JSON-RPC 2.0 |
| `atlas_redislock` | `infra-platform/libs/redislock` | Redis 分布式锁，跨产品共用 |
| `@atlas/chat-ui` | `packages/chat-ui` | 控制台和 TeamFlow 共用的对话组件 |

### 其他产品

| 产品 | 目录 · 包 | 端口 | 说明 |
|---|---|---|---|
| 用户中心 | `usercenter/` · `atlas_usercenter` | 8030，前端 3100 | 用户、组织与权限的唯一数据源，也是统一登录的地方；各应用经开放接口 `/open/v1` 取授权。见 [usercenter/README.md](usercenter/README.md) |
| AI TeamFlow | `teamflow/` · `atlas_teamflow` | 8040，前端 3200 | 让智能体作为团队成员参与研发流程。见 [teamflow/README.md](teamflow/README.md) |

---

## 架构

```mermaid
flowchart LR
  user["员工（浏览器）"]
  nginx["前端网关 nginx<br/>校验登录 · 写身份请求头"]
  uc["用户中心"]

  subgraph ap["Agent 平台"]
    web["控制台"]
    agent["Agent 服务"]
    skill["技能应用"]
    kb["知识库服务"]
    mem["记忆服务"]
    mcp["MCP 网关"]
    mp["模型平台<br/>+ LiteLLM"]
  end

  osb["OpenSandbox"]
  sbx["Shell 沙箱<br/>（原生智能体）"]
  pod["会话 Pod<br/>bridge + CLI"]
  pg[("PostgreSQL")]
  redis[("Redis")]
  oss[("对象存储<br/>会话工作区 · 技能包")]
  up["上游 MCP / REST / gRPC"]
  llm["模型供应商"]

  user --> nginx
  nginx -->|"登录校验"| uc
  nginx --> web & agent
  agent -->|"取 Key、调模型"| mp
  agent --> mcp & kb & mem & skill
  agent -->|"建沙箱、建会话 Pod"| osb
  osb --> sbx & pod
  agent -->|"执行命令"| sbx
  agent -->|"WebSocket"| pod
  pod -->|"调模型、MCP"| mp & mcp
  agent --> pg & redis
  sbx & pod -->|"挂载 /workspace"| oss
  mcp --> up
  mp --> llm
```

图里省略了：各服务都有自己的库（同一个 PostgreSQL 实例上的不同 database）、都向用户中心判权限，以及控制台的管理页经 nginx 直接调技能、知识库、模型、MCP 各自的管理接口。

### 贯穿全局的约定

- **一轮 run 怎么跑**：`POST /threads/{id}/runs` 立刻返回 `202 {run_id}`，执行器在后台跑完整轮。事件先写进 Redis Stream 再推成 SSE，
  游标是事件序号，断线重连自动补发缺口。终态先落库，再发 `run.finished`。不设到点结束的超时：等审批、等用户回答时挂起，回来接着跑。
- **身份**：只有 nginx 网关校验登录（交给用户中心），通过后写入 `X-Uid`、`X-Tenant-Id`，外部带来的身份请求头一律丢弃。
  服务之间内网免令牌，身份在请求头里透传：`X-Uid` 是谁在用，`X-Auth-Uid` 是按谁的权限判，`X-Caller-App` 是哪个应用在调。
  权限都在用户中心：操作码管「能不能做这类事」，数据权限管「对哪些数据」。
- **外部能力都是独立服务，Agent 服务只经 HTTP 调用**：模型只经 LiteLLM（每个智能体一把客户端 Key，由模型平台分配模型）；
  MCP 只经 MCP 网关（上游凭据只在网关里）；技能、知识库、记忆各是一个服务。
- **隔离执行**：Shell 沙箱和会话 Pod 都由 [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) 创建，平台的服务都不持有 K8s 权限。
  会话工作区在对象存储里，经 S3 CSI 挂进 Pod 的 `/workspace`；Git 之类的凭据经 Credential Vault 注入，Pod 里看不到。
- **依赖方向由 CI 守**（import-linter，`make arch`）：`atlas_engine` 是纯库；bridge 只认 ACP、不认识 Agent 服务；`atlas_acp` 是叶子；
  Agent 服务只能用技能应用的 `schemas`；用户中心和其它包互不 import。
- **可观测性**：各服务只把 OTLP 发给 OTel Collector，由它分流：Jaeger 看调用链路，Langfuse 看模型调用。

---

## 核心能力

- **过程可见、可回放**：待办清单、每次工具调用的入参和结果、子智能体的边界、文件产物、token 用量，按序实时推送，断线可续。
- **智能体配置与版本**：保存即产生新版本，历史会话钉在当时的版本上；可以按使用者的权限跑，也可以按智能体自己的数字员工身份跑。
- **子智能体**：主智能体把子任务整个交给另一个智能体，在独立的上下文里执行，只拿回结论；子智能体持有自己的会话，再次委派时接着上次的上下文。
- **人工确认与需求澄清**：高风险工具执行前挂起等人确认，拒绝不会终止本轮，智能体可以换个办法继续；智能体也可以反过来向用户提问。
- **上下文管理**：接近上下文窗口时压缩，并告诉用户「这里压缩过」；原始消息一条不丢。
- **技能、知识库、记忆、MCP**：技能按 `(slug, 版本)` 引用、建会话时拷进会话；知识库按库级和文档级权限检索；记忆跨会话保留；MCP 工具和内置工具走同一套审批。
- **评测**：RAG 评测（含 C-MTEB 数据集导入）和 Agent 评测（含 Terminal-Bench 2），见 [agent-platform/bench/README.md](agent-platform/bench/README.md)。

---

## 目录结构

```
agent-platform/            Agent 平台（Atlas）
├── services/              后端服务，各自独立部署
│   ├── agent/             Agent 服务（atlas_agent），迁移脚本在 migrations/
│   ├── skill/             技能应用（atlas_skill）
│   ├── knowledge/         知识库服务（atlas_knowledge）
│   ├── memory/            记忆服务（atlas_memory）
│   ├── mcpgateway/        MCP 网关（atlas_mcpgateway）
│   └── modelgateway/      模型平台（atlas_modelgateway）
├── libs/                  纯库：engine、host、acp、jsonrpc
├── pod/bridge/            会话 Pod 里的 bridge（atlas_bridge）
├── web/                   控制台
├── bench/                 评测（atlas_bench）
├── demo/                  技术验证的小例子（审批、沙箱、SSE、子智能体、挂起恢复……）
├── doc/                   设计文档与原型（design/、prototype/、principles/）
└── tests/                 Agent 服务、engine、bridge 的测试
usercenter/                用户中心：services/usercenter（atlas_usercenter）、web/
teamflow/                  AI TeamFlow：services/teamflow（atlas_teamflow）、web/
infra-platform/libs/       跨产品共用的基础库（redislock）
packages/chat-ui/          跨产品共用的前端对话组件
deploy/local/              本机 K8s（Docker Desktop）的清单和脚本；服务器部署也复用这些清单
deploy/server/             部署到服务器的脚本（单节点 K8s）
docker/                    镜像：app（所有 Python 服务共用）、frontends、bridge、sandbox、csi-s3
c4-model/                  各系统的 C4 架构图
skills/                    Claude Code 技能（c4-model：按规则生成上面那些 C4 图）
scripts/                   构建、验证用的零散脚本
```

Python 是一个 uv workspace（根目录 `pyproject.toml` 列出全部成员），前端是一个 pnpm workspace（`pnpm-workspace.yaml`；用户中心前端单独安装）。
服务目录和包同名（`services/agent` 即 `atlas_agent`），每个服务都是 `python -m atlas_xxx <子命令>`：`serve`、`migrate`、`register-uc` 等。

## 技术栈

Python 3.13 · FastAPI · SQLAlchemy（async）· Alembic · LangGraph · Celery · PostgreSQL 16 · Redis 7 · Qdrant · MinIO / S3 ·
LiteLLM · MCP · OpenSandbox · Kubernetes · OpenTelemetry · Next.js · React · Ant Design

---

## 本机开发

需要：Python 3.13 和 [uv](https://docs.astral.sh/uv/)、Node.js 和 pnpm、Docker Desktop（开启 Kubernetes）、kubectl、helm。

### 1. 依赖服务和依赖包

```bash
cp .env.example .env    # 模型、对象存储等配置，说明见文件里的注释
make up                 # PostgreSQL :5433 · Redis :6380 · MinIO :9000（控制台 :9001）· Qdrant :6333
                        # · OTel Collector :4318 · Jaeger :16686；各服务的库和 MinIO 的桶自动建好
make sync               # Python 依赖
make web-install        # 前端依赖
```

端口刻意避开了标准端口，免得和本机已有的 PostgreSQL、Redis 冲突。可选：起 Langfuse（:3001），Collector 同时换成分流到 Langfuse 的配置：

```bash
OTEL_COLLECTOR_CONFIG=collector-langfuse.yaml docker compose --profile langfuse up -d
```

### 2. 在本机 K8s 里跑全套

所有服务都跑在 Pod 里，依赖服务用宿主上 `make up` 起的那些；登录和身份请求头只有经过 nginx 网关才有，所以要用全套才能在浏览器里正常使用。

```bash
# 镜像
docker/app/requirements.sh
docker build -f docker/app/Dockerfile    -t atlas-app:dev .
docker build -f docker/bridge/Dockerfile -t atlas-cli-bridge:0.4.0 .

deploy/local/opensandbox.sh     # OpenSandbox：CSI 驱动、控制器、lifecycle server
make migrate                    # Agent 服务的库；其余服务在 Pod 的 initContainer 里自己迁移
deploy/local/apply.sh           # 后端服务（凭据从 .env 建成 K8s Secret，不进 YAML）
deploy/local/frontends.sh       # 三个前端 + nginx 网关 → http://localhost:8080
```

第一次还要在用户中心建首个超级管理员，再把各应用登记到用户中心（不登记，它们调用户中心会 403）：

```bash
kubectl -n atlas-system exec deploy/atlas-usercenter -- python -m atlas_usercenter bootstrap \
  --company 星海科技 --account admin --name 管理员 --email admin@example.com   # 临时密码只输出这一次
# 浏览器打开 http://localhost:8080/uc/login，用临时密码登录、改密码，然后：
deploy/server/register.sh       # 用当前 kubectl 上下文，本机集群一样适用
```

只调试单个服务时也可以在宿主上直接起（`make serve`、`make skill-serve`、`make memory-serve` 等，见 `Makefile`），这时要自己在请求里带 `X-Uid`。

### 3. 检查与测试

```bash
make lint          # ruff
make arch          # 依赖方向（import-linter）
make web-check     # 前端类型检查
make web-gen       # 从运行中的 Agent 服务重新生成前端的 API 类型（结果要提交）
make smoke         # 部署后的冒烟（会真实调用模型）
make regress       # Agent 评测 L1 回归，和基线比较
```

测试**按目录跑，并且用独立的测试库**：

```bash
DATABASE_URL='postgresql+asyncpg://atlas:atlas@localhost:5433/atlas_test' REDIS_URL='redis://localhost:6380/15' \
  uv run pytest agent-platform/tests
uv run pytest agent-platform/services/knowledge/tests
uv run pytest usercenter/services/usercenter/tests     # 各服务的测试各自一个目录
```

> ⚠️ Agent 服务的测试会清空所连数据库里的数据，千万别指向开发库。
> 一次跑全部测试目录（`make test` / `make check`）现在会在收集阶段失败：几个服务的 `conftest.py` 同名，互相顶掉。

---

## 部署到服务器

目标是单节点 K8s（默认 192.168.0.112）。依赖服务（PostgreSQL、Redis、MinIO、Qdrant、OTel Collector、Jaeger、Langfuse）放在 `atlas-infra`，
平台服务在 `atlas-system`，沙箱和会话 Pod 在 `atlas-sbx`、`atlas-cli`。对外入口在 Traefik 上：`atlas.<IP>.nip.io`（三个产品共用的前端网关）
和 `langfuse.<IP>.nip.io`。

```bash
deploy/server/images.sh           # 构建 amd64 镜像并导入节点（third = 第三方镜像）；app、frontends 每次构建打新标签
deploy/server/deploy.sh           # 全部：依赖服务 → OpenSandbox → Agent 服务迁移 → 后端 → 前端 → Ingress → 首个超级管理员 → 登记到用户中心
deploy/server/deploy.sh app       # 改了代码之后：只更新应用
```

应用清单和本机共用 `deploy/local/*.yaml`，差别只在依赖服务的地址（`apply.sh` 的 `render`）。脚本都是幂等的，重复执行只更新有变化的部分。

---

## 文档

- **架构**：[`c4-model/`](c4-model/)。每个系统一个目录（`agent`、`knowledge`、`memory`、`mcpgateway`、`modelgateway`、`skill`、`infra-platform`），
  里面有 `system-context.html`、`container.html`、`component-*.html`、`code-*.html`（按领域拆，画的是目标模型）、`dynamic/`（时序图）和 `deployment-112.html`。
  画法规则在 [`skills/c4-model/`](skills/c4-model/SKILL.md)。
- **设计文档**：[`agent-platform/doc/design/`](agent-platform/doc/design/)（Agent 评测、MCP、记忆、模型），
  原型在 [`agent-platform/doc/prototype/`](agent-platform/doc/prototype/)，LangGraph、OpenSandbox 的原理笔记在 [`agent-platform/doc/principles/`](agent-platform/doc/principles/)。
- **各产品**：[用户中心](usercenter/README.md)、[AI TeamFlow](teamflow/README.md)、[评测](agent-platform/bench/README.md)。

---

## 许可

[MIT](./LICENSE)
