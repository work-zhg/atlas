"""模型管理平台的设置（环境变量前缀 ATLAS_MODELGW_）。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["GatewaySettings", "get_settings"]


def _repo_root() -> Path:
    """仓库根：往上第一个有 uv.lock 的目录。装进镜像后找不到，退回 parents[3]。"""
    here = Path(__file__).resolve()
    return next((p for p in here.parents if (p / "uv.lock").is_file()), here.parents[3])


_REPO_ENV = _repo_root() / ".env"


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATLAS_MODELGW_",
        env_file=_REPO_ENV,
        extra="ignore",
        populate_by_name=True,
    )

    #: 平台库：同一 PG 实例上的独立 database
    database_url: str = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas_modelgateway"

    # ── LiteLLM（模型网关）：平台只调它的管理接口，不转发模型调用 ─────────────
    litellm_base_url: str = "http://127.0.0.1:4000"
    #: LiteLLM 的 master key：只在本服务里，不下发给应用、不进日志。空 = 没配置，管理操作一律失败
    litellm_master_key: SecretStr | None = None
    litellm_timeout_s: float = 30.0
    #: LiteLLM 里有哪些模型的缓存时间（合并展示、算有效模型用）
    litellm_models_ttl_s: float = 30.0
    #: 用户 Key 调 LiteLLM 用的地址（页面上的调用示例）。空 = 还没对集群外开放
    litellm_public_url: str | None = None

    # ── 用户中心 ────────────────────────────────────────────────────────
    uc_base_url: str = "http://127.0.0.1:8030"
    #: 本服务在用户中心的应用编码：内网调开放接口时写进 X-Caller-App。数据编码 Model 挂在它名下
    uc_app_code: str = "model-gateway"
    #: 「用户能用哪些私有模型」的缓存时间：收回授权最多滞后这么久
    perm_cache_ttl_s: float = 30.0
    #: 用户中心不可用时，最多沿用多久以前的结果。超过就按暂时不可用处理
    perm_stale_max_s: float = 3600.0
    #: 管理接口的授权快照（操作码）缓存时间
    authz_ttl_s: float = 30.0

    # ── 客户端 Key ──────────────────────────────────────────────────────
    #: 客户端类型 → 能创建、获取、删除这一类型的应用（用户中心应用编码，即 X-Caller-App）。
    #: 管理员在页面上可以给任何已登记的类型建 Key
    client_types: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "Agent": ["atlas"],
            "Service": ["atlas"],
        }
    )

    # ── 对账 ────────────────────────────────────────────────────────────
    #: 多久对一次账：重算每把 Key 的有效模型，和 LiteLLM 里的不一致就以平台为准修正。
    #: 0 = 不起对账任务（测试、命令行）
    reconcile_interval_s: float = 300.0


@lru_cache
def get_settings() -> GatewaySettings:
    return GatewaySettings()
