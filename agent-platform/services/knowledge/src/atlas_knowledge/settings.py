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

    # ── 认证（与 atlas-config 同一模式：会话交给 atlas-server 校验）────────
    #: dev = 读 X-User-Id（缺省 default_user），操作码全有；uc = 校验 Atlas 登录会话 / 服务账号
    auth_mode: Literal["dev", "uc"] = "dev"
    server_url: str = "http://127.0.0.1:8000"
    #: 双向的服务间令牌：本服务调 atlas-server introspect、atlas-server 调本服务 /internal。
    #: ★ 与 atlas-config 用同一个（atlas-server 侧叫 CONFIG_INTERNAL_TOKEN）。
    internal_token: SecretStr | None = None
    introspect_ttl_s: float = 30.0
    default_user: str = "00000000-0000-0000-0000-000000000001"
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # ── 模型 ─────────────────────────────────────────────────────────────
    #: 默认向量模型（fastembed，本地 ONNX）。★ 与镜像里预下载的一致。
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    #: 重排模型（fastembed 交叉编码）。★ 不预装（约 1GB），首次使用时下载到 model_cache_dir。
    rerank_model: str = "BAAI/bge-reranker-base"
    #: fastembed 模型目录。None = fastembed 默认（<tmp>/fastembed_cache，镜像预装的位置）
    model_cache_dir: str | None = None
    #: 查询向量缓存秒数（同 Dify）
    query_embedding_ttl_s: int = 600

    # ── 入库 ─────────────────────────────────────────────────────────────
    #: 自定义分段长度上限（同 Dify INDEXING_MAX_SEGMENTATION_TOKENS_LENGTH，按字符）
    max_segmentation_length: int = 4000
    upload_max_bytes: int = 50 * 1024 * 1024
    #: 单库文档数上限（入库第 ③ 步配额检查）
    max_documents_per_dataset: int = 5000
    #: 高质量索引的并行组数（同 Dify 的 10 组）
    embedding_parallel: int = 4
    embedding_batch: int = 32

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
