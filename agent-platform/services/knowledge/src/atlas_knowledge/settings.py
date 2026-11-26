"""知识库服务的设置（doc/atlas-rag-design.html）。

★ 对象存储、Redis 与运行时共用同一组变量名（AliasChoices）：本机开发只有一份 .env。
  部署时可以用 `ATLAS_KB_*` 单独给。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["KnowledgeSettings", "get_settings"]


def _repo_root() -> Path:
    """仓库根：往上第一个有 uv.lock 的目录 —— 目录层级调整后不用改。
    装进镜像（site-packages）后找不到，退回原来的 parents[3]，行为不变。"""
    here = Path(__file__).resolve()
    return next((p for p in here.parents if (p / "uv.lock").is_file()), here.parents[3])


_REPO_ENV = _repo_root() / ".env"


def _alias(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class KnowledgeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATLAS_KB_",
        env_file=_REPO_ENV,
        extra="ignore",
        populate_by_name=True,
    )

    #: 知识库库。与运行时、配置服务同一实例上的另一个 database。
    database_url: str = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas_knowledge"
    #: Redis：Celery broker、查询向量缓存、暂停标记、锁。★ 与运行时同实例，不同 db 号。
    redis_url: str = "redis://localhost:6380/3"
    #: 向量库。与记忆共用一个 Qdrant 实例，collection 分开（kb_ 前缀）。
    qdrant_url: str = Field(
        default="http://127.0.0.1:6333",
        validation_alias=_alias("ATLAS_KB_QDRANT_URL", "MEMORY_QDRANT_URL"),
    )
    qdrant_api_key: SecretStr | None = None

    # ── 对象存储（原始文件）────────────────────────────────────────────────
    oss_endpoint: str | None = Field(
        default=None, validation_alias=_alias("ATLAS_KB_OSS_ENDPOINT", "OSS_ENDPOINT")
    )
    oss_bucket: str | None = Field(
        default=None, validation_alias=_alias("ATLAS_KB_OSS_BUCKET", "OSS_BUCKET")
    )
    oss_access_key_id: SecretStr | None = Field(
        default=None, validation_alias=_alias("ATLAS_KB_OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_ID")
    )
    oss_access_key_secret: SecretStr | None = Field(
        default=None,
        validation_alias=_alias("ATLAS_KB_OSS_ACCESS_KEY_SECRET", "OSS_ACCESS_KEY_SECRET"),
    )
    oss_addressing_style: Literal["auto", "path", "virtual"] = Field(
        default="auto",
        validation_alias=_alias("ATLAS_KB_OSS_ADDRESSING_STYLE", "OSS_ADDRESSING_STYLE"),
    )
    oss_region: str = Field(
        default="us-east-1", validation_alias=_alias("ATLAS_KB_OSS_REGION", "OSS_REGION")
    )
    #: 桶内前缀
    oss_prefix: str = "_knowledge"

    # ── 认证（内网身份传递 §3、§8；见 access/auth.py）────────────────────────────
    #: 缺 X-Uid 一律 401，操作码按 X-Auth-Uid 取用户中心授权快照（没有免鉴权的模式）
    #: 授权快照（操作码）的进程内缓存
    authz_ttl_s: float = 30.0
    #: 用户中心（库级权限 = 数据编码 AtlasKnowledgeBase，见 access/perms.py）。内网免令牌：
    #: 请求头 X-Caller-App: atlas 即应用身份
    uc_base_url: str = "http://127.0.0.1:8030"
    #: Atlas 在用户中心的应用编码（知识库属于 Atlas 应用），出站请求头 X-Caller-App 写它
    uc_app_code: str = "atlas"
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    #: 运营接口（/ops/v1）的签名密钥，和运营中心里登记本服务时的一致。空 = 没启用（一律 403）
    ops_api_key: SecretStr | None = None

    # ── 模型 ─────────────────────────────────────────────────────────────
    #: 默认向量模型（fastembed，本地 ONNX）。★ 与镜像里预下载的一致。
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    #: 重排模型（fastembed 交叉编码）。★ 不预装（约 1GB），首次使用时下载到 model_cache_dir。
    rerank_model: str = "BAAI/bge-reranker-base"
    #: fastembed 模型目录。None = fastembed 默认（<tmp>/fastembed_cache，镜像预装的位置）
    model_cache_dir: str | None = None
    #: 远程向量、重排模型的调用超时（秒）。远程模型一律经模型平台，见下一节
    remote_model_timeout_s: float = 60.0
    #: 查询向量缓存秒数（同 Dify）
    query_embedding_ttl_s: int = 600

    # ── 模型平台（agent-platform/doc/design/model-gateway-design.html）───────
    #: 配了就经平台：各用途用平台上事先建好的服务级客户端 Key（客户端类型:编码），
    #: 带着它调模型网关。不持有供应商 Key。空 = 只有镜像里的本地向量、重排模型，没有对话模型
    #: （入库增强、查询理解降级为不做）
    model_platform_url: str | None = None
    #: 模型网关地址（经平台时模型调用连它的 OpenAI 格式入口：{地址}/v1/chat/completions 等）
    model_gateway_url: str | None = None
    #: 入库增强、查询理解用的客户端 Key（如 Service:knowledge-chat）与模型；
    #: 查询理解的模型空 = 同入库增强
    chat_client: str | None = None
    enrich_model: str | None = None
    query_model: str | None = None
    #: 远程嵌入、重排用的客户端 Key：建库时可选的模型 = 这把 Key 的可用模型
    embedding_client: str | None = None
    rerank_client: str | None = None
    #: 客户端 Key 的缓存时间（秒）：Key 被重置后最多滞后这么久（网关回 401 时立即重取）
    model_key_ttl_s: float = 60.0

    # ── LLM（上下文补全、表格摘要、查询理解；doc/knowledge-enterprise-design.html）──
    #: 经模型平台调对话模型的超时（秒）
    llm_timeout_s: float = 60.0
    #: 入库时 LLM 增强的并发调用数
    llm_concurrency: int = 4
    #: 上下文补全时作为固定前缀送给模型的文档正文上限（字符）；超出的长文档改用分段所在的邻近内容
    contextual_doc_chars: int = 30_000

    # ── 入库 ─────────────────────────────────────────────────────────────
    #: 自定义分段长度上限（同 Dify INDEXING_MAX_SEGMENTATION_TOKENS_LENGTH，按字符）
    max_segmentation_length: int = 4000
    upload_max_bytes: int = 50 * 1024 * 1024
    #: 单库文档数上限（入库第 ③ 步配额检查）
    max_documents_per_dataset: int = 5000
    #: 高质量索引的并行组数（同 Dify 的 10 组）
    embedding_parallel: int = 4
    embedding_batch: int = 32
    #: 入库巡检间隔（秒）：API 进程定期把「停在入库中间状态、却没有任务持锁」的文档重新投递
    #: （worker 被杀后的恢复，document/runner.py recover_stalled）。0 = 关
    ingest_sweep_interval_s: float = 60.0

    # ── 遥测（与 atlas-server 同一套约定）───────────────────────────────
    #: 关闭时零副作用：不装 TracerProvider，埋点退化成官方保证的 no-op
    otel_enabled: bool = False
    otel_endpoint: str = "http://127.0.0.1:4318"
    otel_service_name: str = "atlas-knowledge"
    #: 内容采集档位：off = 只记 id / 分数 / 排名；io = 加查询与最终结果正文；
    #: full = 再加各环节候选的正文。正文只写进 langfuse.observation.input/output
    #: （Collector 给 Jaeger 的那一路会删掉），默认 off
    otel_capture_content: Literal["off", "io", "full"] = "off"
    otel_content_max_chars: int = 32_768
    #: 前端「查看链路」的地址模板，{trace_id} 会被替换；空 = 不显示链接
    trace_url_template: str | None = None

    @property
    def storage_configured(self) -> bool:
        return bool(self.oss_bucket)


@lru_cache
def get_settings() -> KnowledgeSettings:
    return KnowledgeSettings()
