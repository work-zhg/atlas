"""配置服务的设置。

★ 对象存储与运行时共用同一组 `OSS_*` 变量名（AliasChoices）：本机开发只有一份
  .env，不必为同一个 MinIO 抄两遍。部署时可以用 `ATLAS_CONFIG_OSS_*` 单独给一组
  凭据 —— 正式环境里只有本服务能写 `_skills/`，运行时只读（设计 §16）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ConfigSettings", "get_settings"]

_REPO_ENV = Path(__file__).resolve().parents[3] / ".env"


def _alias(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class ConfigSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATLAS_CONFIG_",
        env_file=_REPO_ENV,
        extra="ignore",
        populate_by_name=True,
    )

    #: 配置库。★ 与运行时库是同一实例上的**另一个 database**，互不访问（设计 §15）。
    #: ★ 用 localhost 而不是 127.0.0.1：本机 5433 上可能还有一个 brew 起的 Postgres
    #:   只监听 127.0.0.1，localhost 会先解析到 ::1，落在 docker 的 atlas-pg 上 ——
    #:   与运行时 .env 的 DATABASE_URL 同一个库实例。
    database_url: str = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas_config"

    # ── 对象存储 ────────────────────────────────────────────────────────
    oss_endpoint: str | None = Field(
        default=None, validation_alias=_alias("ATLAS_CONFIG_OSS_ENDPOINT", "OSS_ENDPOINT")
    )
    oss_bucket: str | None = Field(
        default=None, validation_alias=_alias("ATLAS_CONFIG_OSS_BUCKET", "OSS_BUCKET")
    )
    oss_access_key_id: SecretStr | None = Field(
        default=None,
        validation_alias=_alias("ATLAS_CONFIG_OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_ID"),
    )
    oss_access_key_secret: SecretStr | None = Field(
        default=None,
        validation_alias=_alias("ATLAS_CONFIG_OSS_ACCESS_KEY_SECRET", "OSS_ACCESS_KEY_SECRET"),
    )
    oss_addressing_style: Literal["auto", "path", "virtual"] = Field(
        default="auto",
        validation_alias=_alias("ATLAS_CONFIG_OSS_ADDRESSING_STYLE", "OSS_ADDRESSING_STYLE"),
    )
    oss_region: str = Field(
        default="us-east-1", validation_alias=_alias("ATLAS_CONFIG_OSS_REGION", "OSS_REGION")
    )

    # ── 认证 ────────────────────────────────────────────────────────────
    #: 运行时调 /internal/* 用的共享令牌。空 = 不校验（仅本机开发，启动时告警）。
    internal_token: SecretStr | None = None
    #: 没带 X-User-Id 时的身份（dev 模式，与运行时同规则）
    default_user: str = "local-dev"
    #: 允许上传人审查自己的技能。★ 默认关：四眼原则（设计 §5.2）。
    #: 本机单人开发时可以打开。
    allow_self_review: bool = False
    #: BFF 上线前管理页由浏览器直连（设计 §13.3）
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # ── 技能包上限（设计 §6.2）──────────────────────────────────────────
    skill_max_bytes: int = 20 * 1024 * 1024
    skill_max_files: int = 500
    skill_max_file_bytes: int = 5 * 1024 * 1024
    skill_description_max_chars: int = 1024
    #: 描述相似度超过它记 warn
    skill_similarity_warn: float = 0.8

    @property
    def storage_configured(self) -> bool:
        return bool(self.oss_bucket)


@lru_cache
def get_settings() -> ConfigSettings:
    return ConfigSettings()
