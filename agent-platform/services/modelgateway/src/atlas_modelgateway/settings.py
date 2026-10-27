"""模型网关的设置（环境变量前缀 ATLAS_MODELGW_）。"""

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
        env_prefix="ATLAS_MODELGW_",
        env_file=_REPO_ENV,
        extra="ignore",
        populate_by_name=True,
    )

    #: 网关库：同一 PG 实例上的独立 database
    database_url: str = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas_modelgateway"

    # ── 上游（本期只有 Higress 上的 AI 路由，§9.5）─────────────────────────
    #: Higress 网关地址。转发 = {higress_base_url}{同一个路径}，Higress 按请求里的模型名选路由
    higress_base_url: str = "http://127.0.0.1:18080"
    #: 网关凭据（§9.3）：调 Higress 时带上，Higress 只认它。只在本服务和 Higress 里，
    #: 不下发给 Atlas 和 Pod，不进日志。空 = 没配置：转发、探测一律失败
    gateway_credential: SecretStr | None = None
    #: 转发时连上游的超时。★ 不设读超时：思考型模型的一次调用可能要好几分钟；
    #:   卡死由调用方按两段数据之间的静默判断（§7.2）
    connect_timeout_s: float = 10.0
    #: 能力探测、接入检查的单次请求超时
    probe_timeout_s: float = 60.0
    #: 请求体上限（长上下文、图片会让请求体有几 MB）
    max_body_bytes: int = 64 * 1024 * 1024
    #: 非流式响应最多读多少用来取用量；超过就不取（用量标为未回报）
    max_meter_bytes: int = 16 * 1024 * 1024

    # ── 用户中心（§6.3）────────────────────────────────────────────────
    uc_base_url: str = "http://127.0.0.1:8030"
    #: 本服务在用户中心的应用编码：内网调开放接口时写进 X-Caller-App。数据编码挂在它名下
    uc_app_code: str = "model-gateway"
    #: 「主体能用哪些模型」的缓存时间：收回授权最多滞后这么久
    perm_cache_ttl_s: float = 30.0
    #: 用户中心不可用时，最多沿用多久以前的判权结果（§6.8）。超过就拒绝
    perm_stale_max_s: float = 3600.0
    #: 管理接口的授权快照（操作码）缓存时间
    authz_ttl_s: float = 30.0

    # ── 维护任务 ────────────────────────────────────────────────────────
    #: 多久跑一次：补写暂存的用量、到期下线、额度预警。0 = 不起维护任务（测试、命令行）
    maintainer_tick_s: float = 30.0


@lru_cache
def get_settings() -> GatewaySettings:
    return GatewaySettings()
