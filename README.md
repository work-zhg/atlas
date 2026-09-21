# Atlas

一个自建的多智能体对话平台：在 Web 工作台里配置智能体（模型、提示词、工具、子智能体、运行限制），
然后与它多轮对话 —— 并把这一轮里**模型说了什么、调了哪些工具、委派了哪些子智能体、写了哪些文件**，
作为一条可回放的事件流实时呈现出来。

![对话工作台](image/对话过程.png)

左侧是会话列表，中间是对话流与执行计划，右侧 Inspector 分「计划 / 工具 / 文件」三页实时展开这一轮的全过程，
底部是本轮 token 消耗。运行中可随时停止 —— 取消是协作式的，不会切断进行中的工具调用。

---

## 核心功能

### 过程可见、可回放
待办清单、每次工具调用的入参与结果、子智能体的委派边界、文件产物、token 消耗，
按严格有序的事件流实时推送。刷新页面、切后台、开多个标签页都能从断点续上 ——
游标是事件序号，断线重连自动补发缺口。

### 智能体配置与版本

![智能体配置](image/智能体配置.png)

- 模型、提示词、工具、子智能体、运行限制均可配置
- **保存即产生新版本**：历史会话永远指向当时那份配置快照，可复现、可回滚
- 草稿态可直接试跑（右侧面板），不落会话、不产生版本
- 工具按需勾选；勾了却缺前提的会明确报出来，而不是静默失效

### 子智能体委派
主智能体把子任务整体交给另一个智能体，在**独立上下文**里执行，只拿回结论 ——
子任务的中间过程不会污染主对话。可从已有智能体直接选取（如上图的 `analyst` / `checker`）。
委派深度结构性封顶为 1。

**子智能体持有自己的会话。** 第二次委派给同一个子智能体时恢复上次的上下文，
不用把背景重抄一遍；它有自己的技能、自己的压缩边界，工作区与主智能体共享
（读得到彼此的产物）。一次委派就是子会话上的一个 run —— 于是它有独立的
事件流、独立的审批、独立的取消。

两点由此而来的约束：

- **并行只能跨不同的子智能体**。同名委派会撞上会话串行锁，工具层直接拒绝
  并给出替代方案，而不是让模型等到超时。需要真并行时把子智能体配成
  `session_mode: ephemeral`（每次新建会话、跑完归档）。
- **重试会看到上次的痕迹**。一次性模型下重试是干净的；持有会话之后不是。
  模型可以用 `fresh=true` 主动要一个干净的开始（旧会话归档而非删除）。

### 人工确认（HITL）
高风险工具执行前挂起等人点头，弹窗展示完整参数。
**拒绝不终止本轮** —— 作为工具结果回给智能体，它可以换个方案继续。
Shell 默认强制进入审批，这条写在 schema 层，绕过前端直接调 API 同样被拦。

### 隔离执行 <sup>接入中</sup>
`SandboxProtocol` 与 `SandboxMiddleware` 的接缝在位，但**当前没有实现方** ——
原先的 Docker 实现已移除，K8s Pod 执行环境（acp 详设的 `providers/pods/`）
尚未接入。期间 `bash` 在工具目录里标为不可用，勾了会明确报出来而不是静默失效。

### 上下文自动压缩
接近上下文窗口时自动摘要，并以事件明确告诉用户「这里压缩过」——
压缩只改变模型视角，原始消息一条不丢。

---

## 架构

![系统架构](image/架构图.png)

> 📊 **[交互式架构图](./doc/atlas-overview.html)** —— 下载后用浏览器打开，
> 支持明暗主题切换，可单独点亮「配置下发」与「状态回传」两条链路。

三层，依赖严格单向：

| 层 | 职责 | 关键约定 |
|---|---|---|
| `web` | 会话工作台 · 智能体编辑器 · 事件归约与渲染 | 只认事件契约与 REST 契约，不感知内核 |
| `server` | 路由 / 编排 / 装配（`executor/build`）/ 执行循环（`executor/runner`）/ 持久化 / 凭据注入 | 唯一接触数据库的地方在 repository 层；`domain/`（spec·events·translator·tool_registry）与 `executor/{build,runner}` 是其中的**纯计算层**，禁碰基础设施 |
| `engine` | = `kernel`（图 + 全部中间件）+ `contracts`（能力协议与错误分类学） | **纯库**：无 Web 框架、无 DB session、无 HTTP、无凭据；kernel 对宿主只许 import contracts |

依赖方向不靠自觉，靠 CI —— import-linter 契约规定 `engine` 里出现 Web 框架或数据库的
import 就红；server 的纯计算层同样有 contract 拦着，「run 一轮」始终能用假模型脱离
数据库单测。

两条关键链路：

- **配置下发**：浏览器 → `api/v1` → `services/` → `executor/build` 装配图 → `executor/runner` 驱动 → `kernel` → 模型网关（`providers/llm`）
- **事件回传**：`runner` 产出 TraceEvent → `executor` → `relay` 写入 Redis Stream → SSE 推回浏览器

---

## 技术栈

Python 3.13 · FastAPI · SQLAlchemy (async) · PostgreSQL 16 · Redis 7 ·
Next.js 15 · Ant Design 5 · LangGraph

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

---

## 许可

[MIT](./LICENSE)

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

**为什么这组测试不可省**：内存后端覆盖主体逻辑（模板、配额、幂等、孤儿
判定），但它有一处刻意不对齐 —— delete 立即生效，而 K8s 是异步的优雅终止。
第一次对着真集群冒烟就抓到三个被它掩盖的 bug，详见该文件头部。
