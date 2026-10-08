"""MCP server 定义与复核结论的来源（技能 / MCP 设计 §8）。

  Settings.mcp_registry = "env"     定义来自 MCP_SERVERS；没有复核表
  Settings.mcp_registry = "config"  定义与复核结论来自 atlas-config（TTL 缓存）

两种来源都交给同一个 SettingsCatalog 做发现与缓存 —— 发现必须在本进程做，
凭据（${ENV} 的真值）只在运行时进程里。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from atlas_config.schemas import McpServerOut, ToolReviewOut

from ..providers.mcp.catalog import SettingsCatalog
from ..providers.mcp.config import McpServerConfig
from ..providers.mcp.review import ReviewIndex
from .client import ConfigClient, ConfigPlaneUnavailable

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from ..config import Settings

logger = logging.getLogger(__name__)

__all__ = ["make_mcp_catalog", "mcp_reviews", "reset_mcp_registry_cache"]


class _Cached:
    """TTL 缓存；拉取失败时继续用上一次的结果（与技能目录同一条规则）。"""

    def __init__(self, ttl_s: float) -> None:
        self._ttl = ttl_s
        self._value: Any = None
        self._at = 0.0

    async def get(self, fetch: Any) -> Any:
        if self._value is not None and time.monotonic() - self._at < self._ttl:
            return self._value
        try:
            self._value = await fetch()
        except ConfigPlaneUnavailable:
            if self._value is None:
                raise
            logger.warning("配置服务不可用，继续使用上一次的 MCP 注册表")
        self._at = time.monotonic()
        return self._value


_servers: dict[str, _Cached] = {}
_reviews: dict[tuple[str, str], _Cached] = {}


def reset_mcp_registry_cache() -> None:
    _servers.clear()
    _reviews.clear()


def _client(settings: Settings) -> ConfigClient:
    token = (
        settings.config_internal_token.get_secret_value() if settings.config_internal_token else ""
    )
    return ConfigClient(settings.config_base_url or "", token or None)


def _to_config(out: McpServerOut) -> McpServerConfig:
    return McpServerConfig(
        name=out.name,
        url=out.url,
        transport=out.transport,
        headers=out.headers,
        enabled=out.enabled,
        call_timeout_s=out.call_timeout_s,
        credential_scope=out.credential_scope,  # type: ignore[arg-type]
        review_required=out.review_required,
    )


async def _server_configs(settings: Settings) -> list[McpServerConfig]:
    if settings.mcp_registry == "env":
        return list(settings.mcp_servers)
    if not settings.config_base_url:
        raise ConfigPlaneUnavailable("MCP_REGISTRY=config 但没有配置 CONFIG_BASE_URL")
    cache = _servers.setdefault(
        settings.config_base_url, _Cached(settings.configplane_status_ttl_s)
    )

    async def fetch() -> list[McpServerConfig]:
        raw = await _client(settings).get("/internal/mcp/servers")
        return [_to_config(McpServerOut.model_validate(item)) for item in raw]

    return await cache.get(fetch)  # type: ignore[no-any-return]


async def make_mcp_catalog(settings: Settings, redis: aioredis.Redis | None) -> SettingsCatalog:
    return SettingsCatalog(
        await _server_configs(settings),
        redis,
        soft_ttl_s=settings.mcp_cache_soft_ttl_s,
        discovery_timeout_s=settings.mcp_discovery_timeout_s,
    )


async def mcp_reviews(settings: Settings, servers: Iterable[McpServerConfig]) -> ReviewIndex:
    """需要复核的 server 的结论。env 模式下没有复核表 —— 返回空。"""
    wanted = [s.name for s in servers if s.review_required]
    if settings.mcp_registry == "env" or not wanted or not settings.config_base_url:
        return {}
    base = settings.config_base_url

    async def one(name: str) -> tuple[str, dict[tuple[str, str], str]]:
        cache = _reviews.setdefault((base, name), _Cached(settings.configplane_status_ttl_s))

        async def fetch() -> dict[tuple[str, str], str]:
            raw = await _client(settings).get("/internal/mcp/reviews", server=name)
            reviews = [ToolReviewOut.model_validate(r) for r in raw]
            return {(r.tool_name, r.digest): r.decision for r in reviews}

        return name, await cache.get(fetch)

    return dict(await asyncio.gather(*(one(n) for n in wanted)))
