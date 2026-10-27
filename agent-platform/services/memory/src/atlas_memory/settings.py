"""记忆服务的设置（环境变量前缀 ATLAS_MEMORY_）。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["MemorySettings", "get_settings"]


def _repo_root() -> Path:
    """仓库根：往上第一个有 uv.lock 的目录。装进镜像后找不到，退回 parents[3]。"""
    here = Path(__file__).resolve()
    return next((p for p in here.parents if (p / "uv.lock").is_file()), here.parents[3])


_REPO_ENV = _repo_root() / ".env"


class MemorySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATLAS_MEMORY_",
        env_file=_REPO_ENV,
        extra="ignore",
        populate_by_name=True,
    )

    #: 记忆库：同一 PG 实例上的独立 database。★ 用 localhost（同 atlas_config 的说明）
    database_url: str = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas_memory"

    # ── 用户中心（数据权限 §3.7、作用域管理的操作码）──────────────────────
    uc_base_url: str = "http://127.0.0.1:8030"
    #: 本服务在用户中心的应用编码：内网调开放接口时写进 X-Caller-App。数据编码挂在它名下
    uc_app_code: str = "memory"
    #: 数据权限校验结果的缓存时间（授权收回最多滞后这么久；只缓存「有权限」的结果）
    perm_cache_ttl_s: float = 30.0
    #: 作用域管理接口的授权快照缓存时间
    authz_ttl_s: float = 30.0

    # ── 输入上限 ──────────────────────────────────────────────────────
    #: 单条条目的字符上限。★ 不是主题大小上限（那个归客户端，§3.3）：只防一次写进一整篇文档
    entry_max_chars: int = 4_000
    #: 一次写入最多删除几条
    write_max_removes: int = 50


@lru_cache
def get_settings() -> MemorySettings:
    return MemorySettings()
