from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .services.mcp import McpServerConfig

# .env 在仓库根，但 alembic 是从 server/ 目录运行的 —— 相对路径会读不到。
# 用绝对路径钉死：server/src/atlas_server/config.py -> parents[3] = 仓库根
_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_REPO_ROOT / ".env", extra="ignore")

    database_url: str
    redis_url: str

    litellm_base_url: AnyHttpUrl = AnyHttpUrl("https://apijp.techstz.com")
    litellm_key: SecretStr
    litellm_timeout_s: float = 120.0

    # §5.2 身份注入的回落值（决策 1：不做鉴权）
    default_user_id: UUID

    default_model: str = "claude-sonnet-5"
    summarizer_model: str = "claude-haiku-4-5"  # §7 压缩 / §8 标题 共用
    titling_timeout_s: float = 3.0

    max_concurrent_runs_per_user: int = 3
    run_events_ttl_s: int = 86_400
    sse_heartbeat_s: int = 15

    models_cache_ttl_s: int = 300

    # §12.2：高风险工具等待人工确认的时限，超时等同拒绝
    approval_timeout_s: int = 600

    # ── cluster 服务（K8s 模板与 Pod 生命周期的唯一所有者）──────────
    #: ★ server 不自己碰 K8s：那份 RBAC 只发给 cluster 一个进程。
    cluster_base_url: AnyHttpUrl = AnyHttpUrl("http://atlas-cluster:8010")
    # ★ 要盖过 cluster 侧的 pod_ready_timeout_s（默认 120s）：ensure 现在会
    #   等 Pod 就绪才返回，这个超时短于它的话，server 会在 Pod 正常启动的
    #   途中放弃，而 cluster 那边还在好好地等 —— 表现是"新建会话总是超时，
    #   但 kubectl 看过去 Pod 好好的"。
    cluster_timeout_s: float = 180.0

    # ── acp 执行环境（acp 详设 §11）────────────────────────────────
    #: 握手超时。Pod 刚被调度时会慢（镜像拉取），比普通 HTTP 宽松。
    acp_ws_connect_timeout_s: float = 10.0
    #: 两条 update 之间的最大静默 —— CLI 卡死的探测器。
    #: 整轮上限仍归 spec.limits.timeout_s，与 native 同源。
    acp_prompt_idle_timeout_s: float = 120.0

    # ── 子智能体委派（detail/subagent.html §06）──────────────────────
    # 准入控制**与普通 run 分开计**，且必须是全局的：acp 子智能体各吃一个
    # Pod，按 run 各自限流的话并发会话数一乘就爆（10 个会话 × 2 = 20 个 Pod）。
    #: 全进程同时执行的子 run 上限。
    subagent_max_running: int = 3
    #: 单个父 run 累计可发起的委派次数。防的是「在每个规划点发一批
    #: 合法尺寸的委派」这种绕过 —— 累计起来远超并发限制。
    subagent_max_per_run: int = 6
    #: 父 run 轮询子 run 终态的间隔。
    subagent_poll_interval_s: float = 0.5

    # 裸网页搜索（services/search.py）。缺失时勾了 web_search 的 run 明确失败。
    serpapi_key: SecretStr | None = None

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
    #: 是否把提示词与回答原文写进 span（§08）。
    #:
    #: ★ 默认关，而且要一直关着。这些内容里有用户的私有代码、.env 里的
    #:   密钥、内部文档片段 —— 一旦进了遥测管道，它们的留存期、访问控制、
    #:   备份策略就与业务数据完全脱钩，而遥测系统的访问面通常宽得多。
    #:   本轮只做「元数据」档位：token 数、模型、耗时、finish_reason、工具名。
    otel_capture_content: bool = False

    # 远程 MCP server 列表（JSON 数组）。凭据写 ${ENV_VAR} 占位符，
    # 真值放环境变量 —— §14 要求凭据不落配置、不进 agent_version.spec。
    # 例：[{"name":"weather","url":"https://x/mcp",
    #      "headers":{"Authorization":"Bearer ${WEATHER_TOKEN}"}}]
    mcp_servers: list[McpServerConfig] = []

    # web 前端的来源。浏览器直连 API（不走 Next 代理 —— SSE 经代理有缓冲风险），
    # 故必须放行跨源。生产改为实际域名，**不要**用 "*"：
    # allow_credentials 与通配符同时使用会被浏览器拒绝，且将来接鉴权就得改。
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
