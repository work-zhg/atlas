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

    #: 模型网关对外的地址（页面上的调用示例）：Atlas 同一个域名的 /llm。空 = 还没对外开放
    gateway_public_url: str | None = None

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

    # ── 模型网关（gateway 子命令） ──────────────────────────────────────
    #: 几个网关实例共用的状态（线路冷却、账号异常、计数、花费、配置版本号）。空 = 只记在进程里
    redis_url: str | None = None
    #: 账号密钥的主密钥（逗号分隔，第一把用来加密）。管理平台加密、网关解密
    secret_keys: SecretStr | None = None
    #: 海外供应商的出网代理（账号的出网方式选「经出网代理」时用）
    egress_proxy: str | None = None
    #: 允许的内网网段：上游确实在内网时（自部署 vLLM）加进来；默认内网地址一律不连
    upstream_allow_cidrs: list[str] = Field(default_factory=list)
    #: 等第一段内容的默认超时（账号上可以单独设）。实测见过近 3 分钟才出第一个字
    first_content_timeout_s: float = 600.0
    #: 已经开始输出后，两段之间多久没有新内容算卡住
    idle_timeout_s: float = 300.0
    #: 压着不发（提交点之前）时多久给调用方发一次保活注释
    keepalive_interval_s: float = 15.0
    #: 一次调用最多试几条线路
    max_attempts: int = 4
    #: 发版时旧实例等手上的回答结束的上限（秒）；超过的连接被断开，调用方按中途中断处理
    drain_timeout_s: int = 120
    #: 配置版本号最多多久看一次；网关状态不可用时多久按时间重新加载一次
    config_check_interval_s: float = 1.0
    config_fallback_reload_s: float = 30.0
    #: 认不出的 Key 记多久（防止错 Key 打满库）
    key_negative_ttl_s: float = 10.0
    #: 调用记录：多久批量写一次、一批多少条、内存里最多攒多少条
    record_flush_interval_s: float = 1.0
    record_batch: int = 200
    record_queue_max: int = 20_000

    #: 新建客户端 Key 时默认分配全部公有对话模型（那一刻的快照）的客户端类型；
    #: 不在里面的「等管理员分配」
    client_key_default_public: list[str] = Field(default_factory=lambda: ["Agent"])

    # ── 定时任务（管理平台） ────────────────────────────────────────────
    #: 多久检查一次异常账号、查一次余额；0 = 不起（测试、命令行）
    account_check_interval_s: float = 300.0
    #: 多久重算一次用户 Key 的有效模型（用户中心那边的变化没有通知）；0 = 不起
    key_refresh_interval_s: float = 300.0
    #: 调用记录保留几个月（按月分区整块删）
    call_retention_months: int = 6

    #: 运营接口的签名密钥（运营中心给）。空 = 运营接口一律 403
    ops_api_key: SecretStr | None = None

    def master_keys(self) -> list[str]:
        raw = self.secret_keys.get_secret_value() if self.secret_keys else ""
        return [k for k in raw.split(",") if k.strip()]


@lru_cache
def get_settings() -> GatewaySettings:
    return GatewaySettings()
