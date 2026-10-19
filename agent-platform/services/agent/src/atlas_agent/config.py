from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .providers.mcp.config import McpServerConfig


# .env 在仓库根，但 alembic 是从 server/ 目录运行的 —— 相对路径会读不到。
def _repo_root() -> Path:
    """仓库根：往上第一个有 uv.lock 的目录 —— 目录层级调整后不用改。
    装进镜像（site-packages）后找不到，退回原来的 parents[3]，行为不变。"""
    here = Path(__file__).resolve()
    return next((p for p in here.parents if (p / "uv.lock").is_file()), here.parents[3])


_REPO_ROOT = _repo_root()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_REPO_ROOT / ".env", extra="ignore")

    database_url: str
    redis_url: str

    litellm_base_url: AnyHttpUrl = AnyHttpUrl("https://apijp.techstz.com")
    litellm_key: SecretStr
    litellm_timeout_s: float = 120.0

    # ── 身份与鉴权（doc/auth-permission-design.html、doc/identity-propagation-design.html）──
    #: 身份一律来自请求头（网关按登录态写入 X-Uid；业务应用调用时写它的用户），见 auth/dependency.py
    #: dev = 缺 X-Uid 用 default_user_id，操作码全有（本机开发、单测）；
    #: uc  = 缺 X-Uid 一律 401；操作码与数据权限按 X-Auth-Uid 取用户中心授权快照
    auth_mode: Literal["dev", "uc"] = "dev"
    #: dev 模式的回落身份
    default_user_id: UUID
    #: 用户中心开放接口（uc 模式必需）。内网免令牌：请求头 X-Caller-App: atlas 即应用身份
    uc_base_url: str = "http://127.0.0.1:8030"
    #: 授权快照（操作码 / 菜单 / 账号状态）的进程内缓存
    authz_snapshot_ttl_s: float = 300.0

    default_model: str = "claude-sonnet-5"
    summarizer_model: str = "claude-haiku-4-5"  # §8 标题（压缩的摘要模型改由智能体配置决定）
    # ── 上下文管理（doc/context-management-design.html）─────────────────
    #: 一批工具结果的总量上限（tokens）：超过时从最大的开始外置到工作区
    context_batch_cap_tokens: int = 80_000
    #: 前缀缓存的有效期。两处共用这一个值：缓存中间件按它打 cache_control；上下文管理
    #: 据它判断「距上次回复超过有效期 → 缓存必然失效，顺手清旧工具结果」。
    #: Anthropic 只接受 5m / 1h 两档。
    prompt_cache_ttl: Literal["5m", "1h"] = "5m"
    #: MCP 工具超过这么多个、或定义总量超过这么多 tokens 时改为按需加载（tool_search）
    deferred_tools_min_count: int = 20
    deferred_tools_min_tokens: int = 10_000
    titling_timeout_s: float = 3.0

    max_concurrent_runs_per_user: int = 3
    run_events_ttl_s: int = 86_400
    sse_heartbeat_s: int = 15
    #: 会话流首次订阅时最多回放多少条历史事件。
    #:
    #: ★ 这个上限是 thread 流**必须**有的东西，不是调优项。订阅单位从 run 换成
    #:   thread 之后，一条流可以横跨几百轮对话 —— 没有它，打开一个老会话就是
    #:   往前端灌几万个事件，页面直接卡死。run 流不需要它是因为它天然只有一轮
    #:   的量。
    #:
    #: ★ 只作用于**首连**（没有 Last-Event-ID 的那次）。带游标的重连要补齐的是
    #:   缺口，那个量由断线时长决定，不该被这里截断。
    sse_thread_replay_events: int = 500
    #: 单个进程允许的并发 SSE 连接数。
    #:
    #: ★ 一个会话一条流，多标签页放大 —— 每条 SSE 占一个 Redis 连接。不设限
    #:   的话耗尽后表现为「所有 HTTP 请求一起变慢」，而根因在一批挂着的长连接。
    #:   宁可对第 N+1 个明确 503：它是可重试的，而慢到超时不是。
    sse_max_connections: int = 200

    models_cache_ttl_s: int = 300

    # ── 横向扩展（doc/horizontal-scaling-design.html）──────────────
    #: 进程角色：api 只接请求与 SSE（run 只入队）；runner 只领取执行 run；
    #: all 两者都做（小规模部署 / 本机开发，即原来的形态）。
    server_role: Literal["api", "runner", "all"] = "all"
    #: 实例 id（run 租约的 owner）。空 = 启动时按 主机名-pid-随机串 生成。
    #: ★ 必须每个进程不同：同名的两个实例会互相续对方的租约。
    instance_id: str = ""
    #: run 租约时长。持有者每 1/3 周期续一次；进程死掉后最多这么久被别的实例接管。
    #: ★ 租约是「进程还活着」的心跳，不是 run 的时长上限：活着就一直续。
    #:   会话串行锁（Redis）的 TTL 也取它 —— 锁跟着租约续，持有者死了锁随之过期。
    run_lease_s: float = 30.0
    #: 领取扫描的间隔。唤醒（Redis 发布/订阅）丢了也不会丢 run —— 扫描兜底。
    run_claim_scan_s: float = 5.0
    #: 本实例同时执行的 run 上限。满了就不再领取（背压），排队的由别的实例领走。
    max_concurrent_runs: int = 32
    #: 停机时等 run 走到安全点交接的上限。超过的直接中止并交接（丢最近一个工具轮次）。
    #: ★ 不是 run 的超时：它限制的是进程多久必须退出。要小于 K8s 的
    #:   terminationGracePeriodSeconds，留出关连接与 flush 遥测的时间。
    drain_timeout_s: float = 60.0
    #: 维护者（全局单例任务）的选主租约与节拍。
    maintainer_lease_s: float = 30.0
    #: 孤儿 Pod 回收的间隔（只有维护者做）。
    pod_reap_interval_s: float = 600.0

    # ── cluster 服务（K8s 模板与 Pod 生命周期的唯一所有者）──────────
    #: ★ server 不自己碰 K8s：那份 RBAC 只发给 cluster 一个进程。
    cluster_base_url: AnyHttpUrl = AnyHttpUrl("http://atlas-cluster:8010")
    # ★ 要盖过 cluster 侧的 pod_ready_timeout_s（默认 120s）：ensure 现在会
    #   等 Pod 就绪才返回，这个超时短于它的话，server 会在 Pod 正常启动的
    #   途中放弃，而 cluster 那边还在好好地等 —— 表现是"新建会话总是超时，
    #   但 kubectl 看过去 Pod 好好的"。
    cluster_timeout_s: float = 180.0
    #: 原生智能体的 Shell 沙箱（doc/native-sandbox-design.html）：Pod 由 cluster 创建。
    #: 关闭时勾了 bash 的智能体在装配期明确报错，不静默少装 execute（§13.2）
    sandbox_enabled: bool = False

    # ── acp 执行环境（acp 详设 §11）────────────────────────────────
    #: 握手超时。Pod 刚被调度时会慢（镜像拉取），比普通 HTTP 宽松。
    acp_ws_connect_timeout_s: float = 10.0
    #: 两条 update 之间的最大静默（在等模型）—— CLI 卡死的唯一探测器。
    #: ★ 一轮本身没有时间上限：CLI 在干活就不中断。open 时下发为 bridge 的 idleS。
    acp_prompt_idle_timeout_s: float = 120.0
    #: 有工具在执行时的最大静默（长命令可以很久没有输出）。下发为 bridge 的 toolIdleS。
    acp_tool_idle_timeout_s: float = 600.0

    # ── SubAgent（detail/subagent.html §06）──────────────────────────
    # 准入控制**与普通 run 分开计**，且必须是全局的：acp SubAgent 各吃一个
    # Pod，按 run 各自限流的话并发会话数一乘就爆（10 个会话 × 2 = 20 个 Pod）。
    #
    # ★ 派 SubAgent 一律挂起，没有「当场等一会儿」：spawn 立刻返回，父 run 落成
    #   suspended，子 run 跑完再叫醒它（与审批同一条路，contracts/suspension.py）。
    #: 全进程同时执行（排队 / 运行中）的子 run 上限。挂起中的不算 —— 它不占进程。
    subagent_max_running: int = 3
    #: 一轮（以用户输入的那个 run 为根的整棵 run 树）累计可派出的 SubAgent 数。
    #:
    #: ★ 按整棵树数：SubAgent 可以继续派 SubAgent、层数不设上限，而挂起中的不占
    #:   上面的并发名额 —— 没有它，一条「A 派 A 再派 A……」的链会一直跑下去。
    #:   也防「在每个规划点派一批合法尺寸的 SubAgent」累计起来远超并发限制。
    subagent_max_per_run: int = 6
    #: 补扫 suspended run 的间隔（唤醒信号丢失时的唯一出路）。
    #:
    #: ★ 正常路径是子 run 终态时**主动**唤醒父 run。这个扫描只兜一种情况：
    #:   子 run 跑完的那一刻进程正好在重启，唤醒信号就此消失，父 run 会
    #:   永远停在 suspended —— 没有报错、没有事件，只是一个永远转圈的界面。
    suspended_sweep_interval_s: float = 60.0

    # ── 会话工作区（对象存储） ──────────────────────────────────────
    # 走 S3 协议：阿里云 OSS 的 S3 兼容端点、MinIO、AWS S3 同一份实现。
    #
    # ★ 没配 bucket = **没有文件能力**。不回落任何内存实现 —— 那会让模型
    #   以为自己有持久工作区，写进去的东西 run 结束即弃，而它收不到任何
    #   提示。宁可让文件工具结构性地不存在（模型看不到 = 不会去调）。
    oss_endpoint: str | None = None
    oss_bucket: str | None = None
    #: 桶内的环境前缀。多环境共用一个桶时靠它隔离。
    oss_root: str = "atlas"
    oss_access_key_id: SecretStr | None = None
    oss_access_key_secret: SecretStr | None = None
    #: MinIO 等自建服务常用 path-style；阿里云 OSS / AWS 用 virtual-hosted。
    oss_addressing_style: Literal["auto", "path", "virtual"] = "auto"
    oss_region: str = "us-east-1"

    @property
    def prompt_cache_ttl_s(self) -> float:
        return 3600.0 if self.prompt_cache_ttl == "1h" else 300.0

    @property
    def workspace_configured(self) -> bool:
        """对象存储是否可用。假则整个文件能力关闭。"""
        return bool(self.oss_bucket)

    # ── 跨会话记忆（记忆设计）──────────────────────────────────────
    #
    # ★ 默认关。记忆有个不对称风险：记对了是锦上添花，**记错了是持续伤害**
    #   —— 一条被错误提炼的「事实」会在之后每一轮被注入，让模型反复基于
    #   错误前提作答，而用户往往不知道问题出在哪（§12 第 1 项）。
    #   所以上线顺序是：先只抽取与存储、纯观察质量，再开检索工具，最后才
    #   开自动注入。本轮只到第一步。
    #
    # ★ 记忆**绝不能成为会话的硬依赖**（§11）：Mem0 不可用时安静降级，
    #   不能让用户发不出消息。
    memory_enabled: bool = False

    #: 抽取用的模型。★ 走 DeepSeek 的 **OpenAI 兼容**端点，与平台自己用的
    #: Anthropic 端点是两码事 —— mem0 的 openai provider 认的是前者。
    memory_llm_base_url: str = "https://api.deepseek.com/v1"
    memory_llm_model: str = "deepseek-flash"
    #: 抽取用的 key。留空则复用 litellm_key（同一个 DeepSeek 账号）。
    memory_llm_api_key: SecretStr | None = None

    #: Embedding 模型（FastEmbed，本地 ONNX）。
    #:
    #: ★ 不用 fastembed 的默认 thenlper/gte-large —— 那个 1.2GB，塞进
    #:   镜像不合算。bge-small-zh 只有 90MB，且针对中文，与本平台的
    #:   实际内容（中文技术对话）对得上。
    #: ★ 换模型要同时改 docker/app/Dockerfile 的预置步骤，否则容器首启
    #:   会去 HuggingFace 下载 —— 在受限网络下那是一次必然失败，而表现
    #:   是「记忆悄悄不工作」。
    memory_embed_model: str = "BAAI/bge-small-zh-v1.5"

    #: 向量库。docker-compose 里的 qdrant。
    memory_qdrant_url: str = "http://127.0.0.1:6333"
    memory_collection: str = "atlas_memory"
    #: Mem0 的变更史库（SQLite）。★ 默认在 ~/.mem0/history.db —— 容器里
    #: 那是易失的，重启即丢。指到挂卷路径上，否则 §10 的「变更史」等于没有。
    memory_history_db: str = "/tmp/atlas-mem0-history.db"

    #: 入队前的廉价过滤（§04）：正文短于这个字数就不值一次抽取的 LLM 调用。
    #: 纯工具执行、一句「好的」，都会被它挡掉 —— 否则记忆服务的成本随会话
    #: 量线性增长而收益极低。
    memory_min_chars: int = 40
    #: 记忆自动注入（上下文管理 §11）：每轮按提问检索相关记忆，作为附件附在请求末尾（不落库）
    memory_auto_inject: bool = True
    memory_inject_top_k: int = 5
    memory_inject_threshold: float = 0.5
    memory_inject_max_chars: int = 5_000
    #: 抽取失败的重试上限，超过进死信（§11）。
    memory_max_attempts: int = 3

    # ── 遥测（可观测性设计 §02）────────────────────────────────────
    #
    # ★ 默认关。opentelemetry-api 在没装 SDK 时是 no-op，所以关着的时候
    #   埋点代码照常执行却不产生任何开销 —— 不需要在调用处写 if。
    #
    # ★ 只往 Collector 发，不直连任何后端。Collector 是唯一的收口点，也是
    #   唯一该做脱敏的地方（§02）：脱敏散在各来源里实现，等于每份都要维护、
    #   每份都可能漏，而新增来源默认是不设防的。
    #   Langfuse / Jaeger 是 Collector 配置里的 exporter，server 不知道
    #   它们存在 —— 这也是「Langfuse 是开关而不是依赖」的落地方式。
    otel_enabled: bool = False
    #: OTLP over HTTP 的基地址（Collector 的 4318）。
    otel_endpoint: str = "http://127.0.0.1:4318"
    otel_service_name: str = "atlas-server"
    #: 内容采集档位（langfuse-integration-design §9）：
    #:   off  —— 只有元数据：步骤、耗时、模型、token、工具名、错误类型
    #:   io   —— 再加 trace 的输入输出（用户消息、最终回答）与工具的参数和结果预览
    #:   full —— 再加每次模型调用的完整输入输出（含系统提示词与历史）
    #:
    #: ★ 默认 off，共享环境开启前要确认合规。这些内容里有用户的私有代码、.env 里的
    #:   密钥、内部文档片段 —— 一旦进了遥测管道，它们的留存期、访问控制、
    #:   备份策略就与业务数据完全脱钩，而遥测系统的访问面通常宽得多。
    #: ★ 兼容旧配置：true → full，false → off。
    otel_capture_content: Literal["off", "io", "full"] = "off"
    #: 单个内容字段的上限（字符）。超出截断并标注，防止一条 span 撑爆后端。
    otel_content_max_chars: int = 32_768
    #: 遥测环境名（local / test / prod），写进 trace，让不同环境的数据分开。None = 不写。
    otel_environment: str | None = None

    @field_validator("otel_capture_content", mode="before")
    @classmethod
    def _content_level_compat(cls, value: object) -> object:
        if isinstance(value, bool):
            return "full" if value else "off"
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
            return "full" if value.strip().lower() in ("true", "1") else "off"
        return value

    # 远程 MCP server 列表（JSON 数组）。凭据写 ${ENV_VAR} 占位符，
    # 真值放环境变量 —— §14 要求凭据不落配置、不进 agent_version.spec。
    # 例：[{"name":"weather","url":"https://x/mcp",
    #      "headers":{"Authorization":"Bearer ${WEATHER_TOKEN}"}}]
    mcp_servers: list[McpServerConfig] = []
    #: 单次 tools/call 的上限（全局默认，单个 server 可用 call_timeout_s 覆盖）。
    #: 超时交给模型（「稍后重试或换一种方式」），不打死整个 run ——
    #: 一个 MCP server 抖一下不该让整轮失败（MCP 详设 §06）。
    mcp_call_timeout_s: float = 60.0
    #: tools/list 的上限。只在冷缓存与软过期刷新时发生。
    mcp_discovery_timeout_s: float = 10.0
    #: 工具定义缓存的软过期：过了之后下一个调用方刷新一次，失败则继续用旧的。
    mcp_cache_soft_ttl_s: float = 600.0
    #: 单个文本结果的上限。★ 必须大于 FilesystemMiddleware 的转存阈值
    #: （80 000 字符），否则会抢先裁掉本可完整转存到工作区的内容。
    mcp_max_result_chars: int = 100_000
    #: MCP server 定义从哪来。env = 上面的 mcp_servers（过渡期）；config = 配置服务的
    #: 注册表（doc/skill-mcp-backend-design.html §8.1）。工具发现两种都在本进程做 ——
    #: 凭据只在运行时进程里。
    mcp_registry: Literal["env", "config"] = "env"

    # ── 配置服务（atlas-config）────────────────────────────────────────
    #: 技能目录与 MCP 注册表的来源。空 = 未接入：
    #:   · 技能引用必须带版本号，保存时不做存在性校验（兼容接入前的行为）
    #:   · 描述以 slug 兜底
    config_base_url: str | None = None
    #: 可变状态（技能 status、下架清单、MCP 定义与复核结论）的缓存时间。
    #: ★ 已发布技能版本的内容不受它影响 —— 版本不可变，永久缓存。
    configplane_status_ttl_s: float = 60.0
    #: 技能版本内容的磁盘缓存。进程重启不该让全部会话重新拉配置。
    configplane_cache_dir: Path = _REPO_ROOT / "var" / "cache" / "configplane"
    #: 单个 agent 所挂技能描述的总长上限 —— 描述常驻每一轮上下文（设计 §7.2）
    skill_index_budget_chars: int = 2000

    # ── 知识库服务（atlas-knowledge，doc/atlas-rag-design.html §10）──────
    #: 空 = 未接入：绑定了知识库的智能体保存与运行都会明确报错（不静默少装能力）。
    #: ★ Pod 里的 ACP CLI 也按这个地址连知识库的 MCP 端点（同在集群内）。
    knowledge_base_url: str | None = None
    #: 知识库 MCP 令牌的签名密钥（与知识库服务的 ATLAS_KB_MCP_SECRET 一致）。
    #: ★ 这是给会话 Pod（不可信代码）的一次性能力凭据，不是服务间令牌 —— 内网去令牌后仍保留
    #:   （内网身份传递 §5.3）。空 = acp 会话不下发知识库 MCP。
    knowledge_mcp_secret: SecretStr | None = None
    knowledge_timeout_s: float = 30.0

    # web 前端的来源。浏览器直连 API（不走 Next 代理 —— SSE 经代理有缓冲风险），
    # 故必须放行跨源。生产改为实际域名，**不要**用 "*"：
    # allow_credentials 与通配符同时使用会被浏览器拒绝，且将来接鉴权就得改。
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    #: 文件预览令牌的空闲有效期。每次命中续期 —— 正在看的页面一直有效，
    #: 放着这么久没有任何请求才失效（doc/detail/file-preview.html §06）。
    preview_token_ttl_s: int = 3600
    #: 预览站点的对外地址（不含路径）。None = 与 API 同址。
    #: ★ 生产建议配独立域名：同址时隔离完全依赖 CSP sandbox 头（§07）。
    preview_base_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
