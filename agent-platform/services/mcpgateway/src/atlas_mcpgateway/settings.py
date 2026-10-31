"""MCP 网关的设置（环境变量前缀 ATLAS_MCPGW_）。

★ Trace 用 OpenTelemetry 的标准环境变量（OTEL_EXPORTER_OTLP_ENDPOINT、OTEL_SERVICE_NAME、
  OTEL_TRACES_SAMPLER …），不在这里另起名字（tracing.py）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
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
    #: 外部客户端（Claude Code、Cursor）连网关的地址，服务地址 = 它 + /mcp/{服务名}。
    #: 只用来在管理端显示配置示例；空 = 还没对集群外开放
    public_url: str = ""

    # ── 上游 ──────────────────────────────────────────────────────────
    #: 发现工具（拉 tools/list、取 OpenAPI、gRPC 反射）的超时
    discovery_timeout_s: float = 15.0
    #: 调上游时建连接的超时。★ 不设读超时：工具调用可能要很久（超时属于流量治理，单独设计）
    connect_timeout_s: float = 10.0

    # ── MCP 代理：定期重新拉取工具定义 ───────────────────────────────
    #: 多久重新拉一次
    refresh_interval_s: float = 600.0
    #: 维护任务多久检查一次「该拉了」。0 = 不起维护任务（测试、命令行）
    maintainer_tick_s: float = 60.0
    #: 管理端「立即刷新」的最小间隔
    manual_refresh_min_interval_s: float = 10.0

    # ── 用户中心 ──────────────────────────────────────────────────────
    uc_base_url: str = "http://127.0.0.1:8030"
    #: 本服务在用户中心的应用编码：内网调开放接口时写进 X-Caller-App。数据编码挂在它名下
    uc_app_code: str = "mcp-gateway"
    #: 「Key 被授了什么」的缓存时间：收回授权最多滞后这么久
    perm_cache_ttl_s: float = 30.0
    #: 管理接口的授权快照（操作码）缓存时间
    authz_ttl_s: float = 30.0

    # ── 智能体 Key ────────────────────────────────────────────────────
    #: 客户端类型 → 能创建、获取、删除这一类型 Key 的应用（用户中心应用编码，即 X-Caller-App）。
    #: 管理员在页面上可以给已登记的类型补申请
    client_types: dict[str, list[str]] = Field(default_factory=lambda: {"Agent": ["atlas"]})


@lru_cache
def get_settings() -> GatewaySettings:
    return GatewaySettings()
