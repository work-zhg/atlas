"""MCP 网关的设置（环境变量前缀 ATLAS_MCPGW_）。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["GatewaySettings", "get_settings"]


def _repo_root() -> Path:
    """仓库根：往上第一个有 uv.lock 的目录。装进镜像后找不到，退回 parents[3]。"""
    here = Path(__file__).resolve()
    return next((p for p in here.parents if (p / "uv.lock").is_file()), here.parents[3])


_REPO_ENV = _repo_root() / ".env"


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATLAS_MCPGW_",
        env_file=_REPO_ENV,
        extra="ignore",
        populate_by_name=True,
    )

    #: 网关库：同一 PG 实例上的独立 database
    database_url: str = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas_mcpgateway"

    # ── 上游（本期只有 Higress，§3.2）──────────────────────────────────
    #: Higress 网关地址。上游 = {higress_base_url}/mcp-servers/{上游服务名}
    higress_base_url: str = "http://127.0.0.1:18080"
    #: 网关凭据（§8.3）：调 Higress 时带上，Higress 只认它。只在本服务和 Higress 里，
    #: 不下发给 Atlas 和 Pod，不进日志和事件。空 = 没配置：登记、刷新、转发一律失败
    gateway_credential: SecretStr | None = None
    #: 从上游拉一次工具定义、做一次接入检查的超时
    discovery_timeout_s: float = 15.0
    #: 转发时连上游的超时。★ 不设读超时：工具调用可能要几十秒，GET 推送流更长；
    #:   调用超时由调用方（Atlas）按服务的 call_timeout_s 判，网关不替它掐
    connect_timeout_s: float = 10.0

    # ── 元数据刷新（§4.1）──────────────────────────────────────────────
    #: 多久重新拉一次工具定义
    refresh_interval_s: float = 600.0
    #: 维护任务多久检查一次「该刷新了」。0 = 不起维护任务（测试、命令行）
    maintainer_tick_s: float = 60.0
    #: 管理端「立即刷新」的最小间隔
    manual_refresh_min_interval_s: float = 10.0

    # ── 用户中心（§5.3）────────────────────────────────────────────────
    uc_base_url: str = "http://127.0.0.1:8030"
    #: 本服务在用户中心的应用编码：内网调开放接口时写进 X-Caller-App。数据编码挂在它名下
    uc_app_code: str = "mcp-gateway"
    #: 「主体能用哪些服务」的缓存时间（Q15）：收回授权最多滞后这么久
    perm_cache_ttl_s: float = 30.0
    #: 管理接口的授权快照（操作码）缓存时间
    authz_ttl_s: float = 30.0


@lru_cache
def get_settings() -> GatewaySettings:
    return GatewaySettings()
