"""TeamFlow 的设置（环境变量前缀 ATLAS_TF_）。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["TFSettings", "get_settings"]


def _repo_root() -> Path:
    """仓库根：往上第一个有 uv.lock 的目录；装进镜像后找不到就退回 parents[3]。"""
    here = Path(__file__).resolve()
    return next((p for p in here.parents if (p / "uv.lock").is_file()), here.parents[3])


_REPO_ENV = _repo_root() / ".env"


class TFSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATLAS_TF_", env_file=_REPO_ENV, extra="ignore")

    #: TeamFlow 库：同一 PG 实例上的独立 database
    database_url: str = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas_teamflow"
    cors_origins: list[str] = ["http://localhost:3200", "http://127.0.0.1:3200"]

    # ── 用户中心（开放接口；内网免令牌，X-Caller-App: teamflow）
    uc_base_url: str = "http://127.0.0.1:8030"
    #: 授权快照、团队级别的进程内缓存
    authz_ttl_seconds: int = 30

    # ── Agent 平台（内网免令牌，X-Uid = 操作人 / 流程发起人，X-Caller-App: teamflow）
    atlas_base_url: str = "http://127.0.0.1:8000"

    # ── 多实例协调：配置后经 Redis 广播唤醒和实时事件；空 = 单进程内存实现
    redis_url: str | None = None

    @field_validator("redis_url")
    @classmethod
    def _empty_is_none(cls, v: str | None) -> str | None:
        return v or None

    #: API 进程内是否跑投递器（收唤醒、扫描）。多实例下开着也安全（节点会话行锁）
    embedded_worker: bool = True
    #: 扫描排着的指令、要取消的 run
    dispatch_sweep_seconds: int = 15
    #: 通知器一轮的间隔
    notify_seconds: int = 10

    # ── 运营接口（运营中心签名调用）
    ops_api_key: SecretStr | None = None


@lru_cache
def get_settings() -> TFSettings:
    return TFSettings()
