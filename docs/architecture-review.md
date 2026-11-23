# Atlas 项目架构与代码深度评审报告

> 初审日期：2026-11-23  
> 复审日期：2026-11-23（基于 122 个文件的第一轮修复后源码级复核）  
> 评审范围：全仓 Monorepo（`agent-platform/`、`infra-platform/`、`usercenter/`、`teamflow/`、`opscenter/`、`packages/`、`deploy/`、`docker/`、`.github/`）

## 概览与总评

Atlas 是一套企业级多产品智能体平台 Monorepo，规模约 **19.8 万行 Python**（不含迁移脚本与生成代码）、**5.9 万行 TypeScript**，包含 **23 个 Python workspace 包**、**5 个 Web/移动端应用** 及 **3 个共享 UI 包**。

**总体评价**：
- **顶层架构设计克制且成熟**，核心抽象（内核框架隔离、无 Checkpointer 分段执行、Runtime/Host 两级正交扩展、分布式租约与优雅交接、黑盒评测）具备一流工程水准。
- **第一轮修复进展显著**：`ruff check/format` 已清零、`lint-imports` 增至 38 条且全绿、`@atlas/chat-ui` 建立起首批单元测试、测试目录补齐至 23 个、核心执行链路与安全边界完成了首轮止血。
- **当前主要风险**：本轮修复新引入了 **1 处 CI 必挂的致命回归**（`POSTGRES_DB: atlas_test` 与 11 处硬编码 `/atlas` 建库冲突），多处 P0 安全与核心 Bug 属于“半修复”（仍存在可实测绕过或可用性倒退），且部署/容器安全与部分微服务高危项尚未动工。

### 实测健康度对比（初审 vs 复审）

| 检查项 | 初审状态 | 复审实测结果 |
|---|---|---|
| `uv run ruff check .` / `format --check` | 5,091 错（含 `.linux-deps`）+ 35 源码错 | **0 报错，1,056 文件格式全过** |
| `uv run lint-imports` | 34 kept（`atlas_skill` 失效、缺 3 包） | **38 kept, 0 broken**（核心已补，少量旧契约仍有遗漏） |
| `@atlas/chat-ui` 单元测试 | 0 条 | **2 文件 8/8 passed**（覆盖仍较薄） |
| 前端 Typecheck + 浏览器 API 检查 | 未入 CI | **4 包 `tsc --noEmit` 0 错，`check:browser-apis` 通过**（`usercenter/web` 仍未入 CI） |
| 全仓测试收集（`scripts/test-all.sh`） | 20 errors，漏 5 目录 | **覆盖全部 23 目录，逐目录收集 22/23 全过**（但 CI 运行期会因缺 `atlas` 库崩毁 9 个目录） |

---

## 一、架构与代码核心优点

1. **框架与业务隔离干净，机械化契约守卫落地扎实**
   - `atlas_engine` 严格分层为 `contracts`（纯 Protocol/契约）← `kernel`（LangGraph 中间件）← `runtime`（装配与驱动），服务端 `atlas_agent` **全仓零直接 import `langchain`/`langgraph`/`atlas_engine.kernel`**。
   - [pyproject.toml](../pyproject.toml#L82-L620) 定义 38 条 `import-linter` 契约且全绿，配合子进程级纯净度测试 [test_engine_purity.py](../agent-platform/tests/test_engine_purity.py#L20-L31) 与 [test_acp_boundary.py](../agent-platform/libs/acp/tests/test_acp_boundary.py#L26-L38)，确保 Pod 叶子包 `atlas_acp` / `atlas_bridge` 零重依赖。

2. **无 Checkpointer 的“单一事实源”分段执行与挂起模型**
   - 刻意不接入 LangGraph Checkpointer，将历史消息、压缩摘要、挂起现场（`WaitingOn`）、审批决策、待办快照统一收口在业务表，审批/提问/浏览器接管/子智能体委派全走同一套 [SuspensionMiddleware](../agent-platform/libs/engine/src/atlas_engine/kernel/middleware/suspension.py#L66-L198)，消除双状态源不一致与反序列化升级难题。
   - 子智能体复用独立子 Run + 父子会话机制，天然继承取消、审批、配额、可观测与孤儿回收能力。

3. **Runtime 与 Host 两级正交扩展，协议隔离清晰**
   - [AgentRuntime](../agent-platform/services/agent/src/atlas_agent/executor/runtime.py#L56-L92) 统一 Native 与 CLI 两种执行范式；本机设备（Device）作为 [CliRuntime](../agent-platform/services/agent/src/atlas_agent/cli/runtime.py#L88-L371) 下与 K8s `PodHost` 平行的 `CliHost` 实现（[cli/host.py](../agent-platform/services/agent/src/atlas_agent/cli/host.py#L54-L93)）。
   - `atlas_host` 不懂 ACP 语义，服务端仅由纯函数 `cli.translate` 翻译为与 Native 同形的 `TraceEvent`；[devicegw/capability.py](../agent-platform/services/devicegw/src/atlas_devicegw/capability.py#L98-L186) 做到真实模型/MCP 凭据不出集群。

4. **面向横向扩展与优雅停机的分布式执行器与网关设计**
   - `POST /runs` 异步化 + Redis Stream + `thread_seq` 游标续传 + 终态先落库再发事件；`FOR UPDATE SKIP LOCKED` 抢单 + `owner` 租约围栏；[main.py](../agent-platform/services/agent/src/atlas_agent/main.py#L49-L68) 捕获 `SIGTERM` 主动关闭 SSE 并执行安全点交接。
   - [redisx.py](../agent-platform/services/agent/src/atlas_agent/redisx.py#L47-L91) 拆分为 `commands` 与 `streams` 两个 `BlockingConnectionPool`，有效隔离 SSE 长阻塞读与短命令池。
   - [modelgateway](../agent-platform/services/modelgateway/src/atlas_modelgateway/gateway/app.py#L153-L205) 控制面与数据面物理分离，具备 `MultiFernet` 信封加密（[crypto.py](../agent-platform/services/modelgateway/src/atlas_modelgateway/crypto.py#L25-L61)）、热路径零查库快照与 `LazyStream` 首块提交前透明切线。

5. **高内聚基础库、防御性注释与黑盒评测体系**
   - `infra-platform/libs/*` 抽象克制：[atlas_redislock](../infra-platform/libs/redislock/src/atlas_redislock/lock.py#L48-L63)（全 Lua 原子脚本 + Python 3.13 `uncancel()` 看门狗）、[atlas_appconfig](../infra-platform/libs/appconfig/src/atlas_appconfig/service.py#L92-L101)（声明式配置 + `TypeChanged` 滚动发布防呆）、[atlas_tenancy](../infra-platform/libs/tenancy/src/atlas_tenancy/filter.py#L78-L126)（双层 ORM 过滤 + Fail-Closed）。
   - 关键代码普遍使用 `★` 沉淀真实线上排障与反直觉决策根因；Prompt Caching 全链路协同（7 槽位中间件栈 [kernel/stack.py](../agent-platform/libs/engine/src/atlas_engine/kernel/stack.py#L29-L61) + 缓存层置最内层）；`agent-platform/bench` 坚持黑盒隔离，覆盖 L0 冒烟、L1 bootstrap 显著性回归、Terminal-Bench 2 与混沌注入。
   - 可靠传输实现严谨：[bridge/upstream/outbox.py](../agent-platform/pod/bridge/src/atlas_bridge/upstream/outbox.py#L204-L256) 断线保留控制帧与未答复审批请求；[devicelink/mux.py](../agent-platform/libs/devicelink/src/atlas_devicelink/mux.py#L60-L73) 实现 4 级优先级多路复用。

---

## 二、第一轮修复逐项核验状态

### 已彻底修复（✅）

1. **知识库 RRF 分数写回 `Hit`**：[pipeline.py](../agent-platform/services/knowledge/src/atlas_knowledge/retrieval/pipeline.py#L803-L806) 已覆盖 `RetrievedSegment | Hit` 且不污染前置漏斗数据，配套单测见 [test_retrieval_enterprise.py](../agent-platform/services/knowledge/tests/test_retrieval_enterprise.py#L113-L123)。
2. **`agent-platform/tests/conftest.py` 误清开发库防护**：[conftest.py](../agent-platform/tests/conftest.py#L50-L65) 新增 session autouse 校验，库名不以 `_test` 结尾直接 `pytest.exit(..., 2)`。
3. **Ruff Lint/Format 清零 & 漏掉的 5 个测试目录补齐**：[pyproject.toml](../pyproject.toml#L487-L489) 排除 `.linux-deps`，[testpaths](../pyproject.toml#L532) 补全全部 23 个目录。
4. **`atlas_skill` 失效的 `import-linter` 契约**：[pyproject.toml](../pyproject.toml#L212-L218) 改为整包禁止 + 仅放行 `atlas_skill.schemas`，补齐 `atlas_devicelink` / `atlas_connect` / `atlas_devicegw` 隔离契约。
5. **Agent 流式热路径每帧查取消位**：[runner.py](../agent-platform/libs/engine/src/atlas_engine/runtime/runner.py#L403-L415) 改为 0.25s 节流 + `_CancelWatch`。

### 部分修复，仍存在绕过、新回归或边界缺陷（⚠️）

#### 1. 【P0 · 新引入 CI 必挂】CI 改用 `POSTGRES_DB: atlas_test`，但 11 处测试硬编码连 `/atlas` 建库
- [.github/workflows/ci.yml](../.github/workflows/ci.yml#L20) 将 Postgres 初始化库改为 `atlas_test`，CI 容器内**不存在 `atlas` 数据库**。
- 全部 8 个微服务 `conftest.py` 及 `agent-platform/tests` 中 3 处跨服务测试（共 11 处）仍硬编码 `_ADMIN_URL = ".../atlas"` 建库：
  - [knowledge/tests/conftest.py](../agent-platform/services/knowledge/tests/conftest.py#L29)、[mcpgateway/tests/conftest.py](../agent-platform/services/mcpgateway/tests/conftest.py#L27)、[skill/tests/conftest.py](../agent-platform/services/skill/tests/conftest.py#L25)、[memory/tests/conftest.py](../agent-platform/services/memory/tests/conftest.py#L22)、[modelgateway/tests/conftest.py](../agent-platform/services/modelgateway/tests/conftest.py#L23)、[opscenter/tests/conftest.py](../opscenter/services/opscenter/tests/conftest.py#L28)、[teamflow/tests/conftest.py](../teamflow/services/teamflow/tests/conftest.py#L21)、[usercenter/tests/conftest.py](../usercenter/services/usercenter/tests/conftest.py#L23)
  - [gateway_env.py](../agent-platform/tests/gateway_env.py#L77)、[test_memory.py](../agent-platform/tests/test_memory.py#L40)、[test_skill_mcp_runtime.py](../agent-platform/tests/test_skill_mcp_runtime.py#L280)
- **后果**：CI 运行 `scripts/test-all.sh` 时这 9 个目录会全部因 `database "atlas" does not exist` 崩毁。此外，`.linux-deps/` 虽已写入 `.gitignore`，但 **git 索引中仍有 218 个文件未 `git rm -r --cached` 移出跟踪**；且仍有 4 个服务测试目录残留 `from conftest import ...`（直接跑 `uv run pytest` 仍报 21 errors）。

#### 2. 【P0 · 高危安全绕过】`opscenter` 与 `devicegw` 路径穿越校验仍可绕过，且存在验签 401
- **`opscenter`**：[gateway.py](../opscenter/services/opscenter/src/atlas_opscenter/gateway.py#L262-L268) 与 [api/systems.py](../opscenter/services/opscenter/src/atlas_opscenter/api/systems.py#L48-L52) 仅对 Starlette 解码后的原始串执行 `any(unquote(seg) in (".", "..") for seg in path.split("/"))`，未对 `httpx.URL` 规范化后的 `url.path` 复核白名单：
  - **`..%23` / `..%3f` 绕过（实测成立）**：请求 `POST /api/systems/usercenter/ops/apps/..%23`，切段得 `"..#"` 绕过 `has_dot_segments` 并匹配白名单，`httpx.URL(".../ops/v1/apps/..#")` 剥离 `#` 后将 `..` 折叠为 **`/ops/v1`** 并完成 HMAC 签名发出。
  - **`..%252f` 绕过（实测成立）**：先 `split("/")` 再 `unquote` 导致 `..%2f..%2ftenants` 作为单段放行；`\\`、`//`、`..;` 未拦截；`_RULES` 用 `$` 而非 `\Z`，尾随 `%0a` 触发未捕获 `httpx.InvalidURL` 返回 500。
  - **编码口径不一致致 401**：[gateway.py](../opscenter/services/opscenter/src/atlas_opscenter/gateway.py#L170) 对未解码的 `url.raw_path` 签名，而 [opsapi/guard.py](../infra-platform/libs/opsapi/src/atlas_opsapi/guard.py#L34) 对已解码的 `request.url.path` 验签，含中文/空格路径必报 401。
- **`devicegw`**：[capability.py](../agent-platform/services/devicegw/src/atlas_devicegw/capability.py#L167-L174) 同款切段顺序漏洞，实测 `..%252f..%252finternal`、反斜杠 `..%5c`、`%3f`/`%23` Query/Fragment 注入均可绕过并携带真实上游凭据发往上游。
- **`devicegw` 内部鉴权细节**：[app.py](../agent-platform/services/devicegw/src/atlas_devicegw/app.py#L169) `.removeprefix("Bearer ")` 不强制前缀存在，WS 侧 [relay.py](../agent-platform/services/devicegw/src/atlas_devicegw/relay.py#L104) 仍用 `!=` 而非 `hmac.compare_digest`，`internal_token=""` 默认 fail-open。

#### 3. 【P0 · 可用性倒退】`modelgateway` 数据面接入 `TenantGate` 破坏 DB 故障降级契约
- [gateway/app.py](../agent-platform/services/modelgateway/src/atlas_modelgateway/gateway/app.py#L95-L97) 已接入 `TenantGate(sm).is_active`，但 [opsapi/state.py](../infra-platform/libs/opsapi/src/atlas_opsapi/state.py#L140-L150) **无异常降级（沿用旧缓存）且无单飞锁防缓存击穿**。
- **后果**：破坏了数据面“DB 闪断仍继续服务”的原有高可用设计，DB 短暂不可用时 5 秒缓存过期后全量推理请求直接 500；热租户每 5 秒还会并发击穿 DB 连接池。此外，多副本间未走 Redis 失效广播。

#### 4. 【P0 · 新边界 Bug】自动压缩 `summary_upto` 在 `keep_recent_turns == 1` 时永久丢弃摘要
- [compactor.py](../agent-platform/libs/engine/src/atlas_engine/kernel/context/compactor.py#L235-L241) 与 [compaction.py](../agent-platform/services/agent/src/atlas_agent/services/compaction.py#L61-L78) 已改按 `kept_from` 反查前一条消息时间，解决了默认 `keep_recent_turns=3` 吞历史的 Bug。
- **残留隐患**：
  1. 当 `keep_recent_turns == 1` 且 `resume=False` 时，[segment.py](../agent-platform/services/agent/src/atlas_agent/executor/segment.py#L229-L232) 剥离本轮用户输入并丢弃其 `message_uuid`，[runner.py](../agent-platform/libs/engine/src/atlas_engine/runtime/runner.py#L203-L204) 构造的 `HumanMessage` 为随机 UUID，`last_before` 查不到直接丢弃摘要 → 退化为每轮重压。
  2. [repositories/run.py](../agent-platform/services/agent/src/atlas_agent/repositories/run.py#L147-L149) 的 `Message.created_at < first` 在同时间戳消息下会回退跳过 `head[-1]`，若刚好切在 `tool_use` / `tool_result` 之间，下轮留下孤立 `tool_result` 触发 API 400；同文件 `history()`（L133）也缺少 `Message.id.desc()` 次级排序。

#### 5. 【P1 · 并发与一致性残留】取消、子智能体与会话删除仍有未闭合窗口
- **取消竞态**：`lock_unclaimed`（[run_record.py](../agent-platform/services/agent/src/atlas_agent/repositories/run_record.py#L69-L79)）已覆盖 `("suspended", "queued")`，但 [scheduling.py](../agent-platform/services/agent/src/atlas_agent/executor/scheduling.py#L216) 仍只判 `in SUSPENDED_STATUSES` 漏掉 `"queued"`——在 `requeue` 后或排队时取消（尤其租户停用/满额时）会卡死在 `queued` 且锁永不释放；另 [continuation.py](../agent-platform/services/agent/src/atlas_agent/executor/continuation.py#L265) 的 `relay.emit` 在 `suppress` 外，Redis 闪断会跳过释放锁。
- **子智能体事务与锁**：建 Run 与首条 Message 已合单事务并加租户级准入锁，但 [subagent.py](../agent-platform/services/agent/src/atlas_agent/services/subagent.py#L217) 的 `_resolve_thread` 在拿准入锁**之前**就提前创建子会话、拷 S3 技能甚至归档旧持久会话（`fresh=True`）并 `commit()`，并发被拒时残留孤儿会话或误归档旧会话。
- **会话级联删除**：已补齐 BFS 子树删除与全部 5 张关联表（[repositories/thread.py](../agent-platform/services/agent/src/atlas_agent/repositories/thread.py#L346-L413)），但：① [services/thread.py](../agent-platform/services/agent/src/atlas_agent/services/thread.py#L514-L535) 检查在途 run 与删表存在 TOCTOU 竞态，且未释放子孙会话的 CLI Pod / DeviceSession；② 单删子会话时 [repositories/thread.py](../agent-platform/services/agent/src/atlas_agent/repositories/thread.py#L400) 按 `RunEvent.thread_uuid` 会漏删记在根会话下的子 run `RunEvent`；③ `count_subagent_threads`（L253）仍只数直接一层。

#### 6. 【P1 · Redis 性能与连接池盲区】
- **CLI 在线审批 PubSub 独占 `commands` 短命令池**：[segment.py](../agent-platform/services/agent/src/atlas_agent/executor/segment.py#L346) 把 `shared_redis()` 传给 `CliRuntime` → `DecisionChannel.listen` 长阻塞 `subscribe`，可能挤压短命令池。
- `EventRelay.publish`（[relay.py](../agent-platform/services/agent/src/atlas_agent/stream/relay.py#L116-L153)）仍为 2 RTT（`EVAL` 未用 `register_script` + `pipeline`）；`tail_thread` 分页消除了内存尖峰，但末尾重连仍扫完 10,000 条（20 次 `XRANGE`）且缺坏 JSON 容错；[services/workspace.py](../agent-platform/services/agent/src/atlas_agent/services/workspace.py#L219) 仍请求级 `make_redis()`；[stream/gate.py](../agent-platform/services/agent/src/atlas_agent/stream/gate.py#L56-L79) `admit()` 与 `guard()` 间存在最后槽位竞态。

#### 7. 【P1 · 前端 `@atlas/chat-ui`】补丁未彻底且引入 React 批处理竞态
- **React `setState` updater 内写 ref 导致滞后读**：[useThreadStream.ts](../packages/chat-ui/src/chat/useThreadStream.ts#L106-L113) 在 `setState(prev => ...)` 内更新 `stateRef.current`，而 [sse.ts](../packages/chat-ui/src/lib/sse.ts#L172-L174) 同帧调用 `onTurnEnd`，读到的仍是未含终态事件的旧状态。
- **判别联合类型仍被架空**：[events.ts](../packages/chat-ui/src/lib/events.ts#L538-L540) 仍允许 `UnknownTraceEvent.type: string`，[run-reducer.ts](../packages/chat-ui/src/lib/run-reducer.ts#L552) **23 处 `as unknown as` 清零数为 0**；`lastSeqRef` 死代码（[useThreadStream.ts](../packages/chat-ui/src/chat/useThreadStream.ts#L53-L109)）未删；[sse.ts](../packages/chat-ui/src/lib/sse.ts#L188-L193) 重试耗尽仍先误报一次 `onDisconnect`；静默会话永久空洞会无限回退重放；新增 8 条单测对 850 行核心业务归约逻辑仍为 0 覆盖。

#### 8. 【P1/P2 · 其他半修复项】
- **`opsapi/signature.py`**：4 个运营头已进签名（[signature.py](../infra-platform/libs/opsapi/src/atlas_opsapi/signature.py#L38-L70)），但未拒绝 header 中的 `\n`/`\r`，普通 `dict` 传混合大小写头会静默漏读。
- **`mcpgateway` `TokenCache`**：已加入 `sha256(secret)`（[credentials.py](../agent-platform/services/mcpgateway/src/atlas_mcpgateway/upstream/credentials.py#L143)）防蹭票，但未含 `tenant_uuid`、内存无界淘汰、全局单一 `asyncio.Lock` 可被单个慢上游 15s 阻塞全网关换票。
- **`atlas_tenancy/purge.py`**：外层 `DELETE` 已加 `tenant_uuid` 防越权（[purge.py](../infra-platform/libs/tenancy/src/atlas_tenancy/purge.py#L69-L74)），但仍不兼容 MySQL（Error 1093/1235），且复合主键首列为 `tenant_uuid` 时会突破 `limit` 批次上限（PG 建议改为 `sa.tuple_(*pks).in_(...)`）。
- **`import-linter` 少量遗漏**：[pyproject.toml](../pyproject.toml#L546-L618) 中 `atlas_tenancy` / `atlas_opsapi` / `atlas_opscenter` 与早期叶子/服务契约仍漏填部分新增包（建议微服务间改用 `type = "independence"`）。

---

## 三、尚未处理的架构与代码债务（❌）

### 1. 高危安全与部署生产化
- **`mcpgateway` 3 处遗留**：上游凭据（含 mTLS 私钥）与全部 API Key 仍明文落库（[mcpgateway/db/models.py](../agent-platform/services/mcpgateway/src/atlas_mcpgateway/db/models.py#L20-L201)）；MCP 代理未注入受控客户端仍可绕过 SSRF（[upstream/mcp.py](../agent-platform/services/mcpgateway/src/atlas_mcpgateway/upstream/mcp.py#L37-L50)）；`uq_mcp_api_key_client` 仍漏 `tenant_uuid`。
- **`deploy/` 与 `docker/` 全未触碰**：
  - [08-sandbox-network.yaml](../deploy/local/08-sandbox-network.yaml#L13-L15) 端口级放行 `atlas-cli` Pod 访问 `knowledge:8050` / `mcpgateway:8070`，在内网免令牌架构下存在跨租户越权；
  - [docker/app/Dockerfile](../docker/app/Dockerfile#L1-L99) 10 服务 + 90MB ONNX 模型打成单胖镜像且以 **root** 运行；
  - 数据库弱口令 `atlas:atlas` 明文遍布 12+ 清单，内网 IP `192.168.0.112` 硬编码 8 处；
  - 所有后端 Deployment 均无 `resources.limits`、12/13 无 `livenessProbe`、全部 `replicas: 1`（`atlas-server` 为 `Recreate` 停机发布）；运营中心 `/ops` 与租户同域且用户中心 Cookie 未启 `Secure`。
- **微服务安全缺口**：`usercenter` `/open/v1` 零认证（[usercenter/api/deps.py](../usercenter/services/usercenter/src/atlas_usercenter/api/deps.py#L146-L166)）；`knowledge` MCP 令牌永不过期（[knowledge/api/mcp.py](../agent-platform/services/knowledge/src/atlas_knowledge/api/mcp.py#L72-L83)）且上传先全量 `f.read()` 进 RAM（[datasets.py](../agent-platform/services/knowledge/src/atlas_knowledge/api/datasets.py#L409-L417)）；`memory` `register` 无鉴权可抢注 Owner（[instances.py](../agent-platform/services/memory/src/atlas_memory/api/instances.py#L108-L123)）；`skill` zip 炸弹先全量解压（[package.py](../agent-platform/services/skill/src/atlas_skill/check/package.py#L125-L128)）；`teamflow` 缺失多租户/运营隔离且仍在 `FOR UPDATE` 行锁内调外部 HTTP（[process/service.py](../teamflow/services/teamflow/src/atlas_teamflow/process/service.py#L180-L188)、[atlas_proxy.py](../teamflow/services/teamflow/src/atlas_teamflow/api/atlas_proxy.py#L165-L171)）。

### 2. 核心稳定性与隐藏泄漏
- [segment.py](../agent-platform/services/agent/src/atlas_agent/executor/segment.py#L568-L570) 终态落库后 `relay.emit` 无降级捕获，Redis 闪断会阻断 `_follow()` 漏唤醒父 run。
- **[新发现泄漏]** [filesystem.py](../agent-platform/libs/engine/src/atlas_engine/kernel/middleware/filesystem.py#L923-L927) 每次实例化 `FilesystemMiddleware`（每段 run 建图）都会新建一个**从未被引用也从未 `shutdown()`** 的 `ThreadPoolExecutor(max_workers=4)`，且保留 214 行未用 `deepagents` 权限死代码。
- [agent/db/models.py](../agent-platform/services/agent/src/atlas_agent/db/models.py#L399) 与 L1249 存在同名检查约束 `browser_host_kind_enum`；部分唯一索引仅配 `postgresql_where` 在 MySQL 下会退化。
- [devicelink/mux.py](../agent-platform/libs/devicelink/src/atlas_devicelink/mux.py#L113) 接收队列无界且不防对端信用超发；[bridge/outbox.py](../agent-platform/pod/bridge/src/atlas_bridge/upstream/outbox.py#L68-L69) 字符计数当字节计数且淘汰为 O(N²)。

### 3. 架构治理与可维护性
- **跨服务样板复制漂移**：错误响应分裂为 3+1 套格式，身份中间件、UserCenter 客户端、`db/types.py`、`db/session.py` 复制 5~8 份，仍存在 `memory`/`modelgateway` 每次未命中新建 `httpx.AsyncClient`、`teamflow` 漏分页等漂移缺陷，亟需抽离轻量 `atlas-service-kit`。
- **千行上帝文件未拆**：后端 `filesystem.py`（1536 行）、`agent/db/models.py`（1299 行）、`memory/store.py`（1038 行）、`teamflow/process/service.py`（1033 行）、`usercenter/app/service.py`（1008 行）、`usercenter/perm/data.py`（1005 行）；前端 `AgentStudio.tsx`（1369 行）、`McpService.tsx`（1055 行）、`DatasetPage.tsx`（935 行）。
- **前后端契约与工程治理**：仅 2/9 服务生成 OpenAPI 类型且无 CI 漂移检查；`usercenter/web` 仍游离在根 `pnpm-workspace.yaml` 与 CI 之外；`mypy` 仍未纳入 dev group 与 CI；`@atlas/chat-ui` 缺 `exports` 封装；L1 回归基线仍在被忽略的 `var/` 中。

---

## 四、最新优先级整改路线图

### P0（立即阻断修复）
1. **修通 CI 并清理 Git 脏索引**：将 11 处 `_ADMIN_URL` 的 `/atlas` 改为 `/postgres`；对 `.linux-deps/` 执行 `git rm -r --cached .linux-deps` 并清理根目录 `:memory:.ses`。
2. **封死代理路径穿越与验签 401**：`opscenter` 与 `devicegw` 代理入口拒绝 `%`/`\`/`?`/`#` 控制字符并对 `httpx.URL` 规范化后的 `url.path` 复核前缀与白名单；统一 `opsapi/guard.py` 与 `opscenter/gateway.py` 的路径编码口径；`devicegw` 强制校验 `Bearer ` 前缀、全用 `hmac.compare_digest` 并关闭空 token fail-open。
3. **补救 `modelgateway` 数据面可用性**：为 `TenantGate.is_active()` 增加单飞锁与 DB 异常降级沿用旧缓存，恢复数据面容灾能力。
4. **收口 Agent 核心边界 Bug**：
   - `SegmentLoader` 透传本轮用户输入 `message_uuid` 给 `HumanMessage(id=...)` 修复 `keep_recent_turns=1` 摘要丢失；`RunRepository.history` 补 `Message.id.desc()` 次级排序，压缩水位改为消息 ID/时间戳联合游标。
   - `RunWorker.cancel` 放行 `"queued"`；`segment.py:568` 与 `continuation.py:265` 的终态 `relay.emit` 包裹异常降级。
5. **消除 `mcpgateway` 剩余高危安全项 & 沙箱网络越权**：`mcpgateway` 凭据/Key 接入信封加密、修复 MCP 代理 SSRF 与 `uq_mcp_api_key_client` 租户列；推进 CLI Pod 经网关公开入口访问并删除 `08-sandbox-network.yaml` 内网端口白名单。

### P1（短期治理）
1. **修复前端流状态机竞态与测试深度**：`useThreadStream.ts` 改为同步算 `next` 更新 `stateRef.current` 再 `setState(next)`；清理 `lastSeqRef` 死代码、收紧判别联合消除 23 处强转、修复 `onDisconnect` 重试耗尽误触发与永久空洞，补齐 `run-reducer.ts` 核心业务单测。
2. **修补并发与资源泄漏**：移除 `filesystem.py:923` 泄漏的未用 `ThreadPoolExecutor`；CLI `DecisionChannel` 改用独立/streams Redis 池；`SubAgentService` 将 `_resolve_thread` 移入准入锁或延迟提交；补齐 `ThreadService.delete` 子树 Pod 释放与单删子会话 `RunEvent` 清理；修复 `atlas_tenancy/purge.py` 复合主键元组删除。
3. **容器与 K8s 生产加固**：`docker/app/Dockerfile` 切非 root `USER`；迁移 `atlas:atlas` 入 Secret；补齐全部后端 `limits`、`livenessProbe` 与多副本滚动升级策略。
4. **抽离 `atlas-service-kit` 止血复制漂移**：统一错误响应体、身份中间件、`UCClient`（带连接池与分页）、`db/types.py`、`AddressGuard`；补齐 `teamflow` 多租户、`knowledge` MCP 令牌过期与流式上传限流、`memory` 注册鉴权。

### P2（中期演进）
1. `import-linter` 微服务间升级为 `type = "independence"`，清理 `knowledge/tests` 等残留 `from conftest import` 实现单进程全仓 `pytest`。
2. 拆解千行上帝文件（`agent/db/models.py`、`filesystem.py`、`AgentStudio.tsx`、`McpService.tsx`、`DatasetPage.tsx` 等），清理 `db/models.py` 约束重名。
3. 将 `usercenter/web` 纳入根 `pnpm-workspace` 与 CI，恢复 `mypy` 检查，推进全服务离线 OpenAPI 类型生成与可版本化的 L1 回归基线。
