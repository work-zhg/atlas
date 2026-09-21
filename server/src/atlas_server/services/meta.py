"""元数据服务：model_catalog ⋈ 网关巡检结果（文档 §11.4）。

网关不可达时**不报错** —— 退化为 catalog 中的存量 is_available，
并在响应里把 gateway_reachable 置 false，让前端能提示"可用性未刷新"。
这是 §13.2「不允许静默降级」的应用。
"""

from __future__ import annotations

import json
import logging

import redis.asyncio as aioredis
from atlas_engine.contracts import ModelUnavailable
from atlas_server.providers.llm.gateway import GatewayConfig, list_models
from atlas_server.domain.tool_registry import BUILTIN_TOOLS, WEB_SEARCH_TOOL
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..repositories.model_catalog import ModelCatalogRepository
from ..schemas.meta import ModelInfo, ModelListResponse, ToolInfo, ToolListResponse
from .mcp import McpService

logger = logging.getLogger(__name__)

_MODELS_CACHE_KEY = "models:available"

# 内置工具目录从 engine 的注册表派生（tool_registry.py）——
# 此前这里手写一份，与 SUPPORTED_TOOLS / _middleware_for 三处人肉同步，
# 目录「说谎」（标可用但 build_agent 不认）就是那时产生的。
_BUILTIN_TOOLS: tuple[ToolInfo, ...] = tuple(
    ToolInfo(
        name=t.name,
        kind="builtin",
        display_name=t.display_name,
        description=t.description,
        available=t.implemented,
        note=t.note,
        model_tool_names=list(t.approval_targets()),
    )
    for t in BUILTIN_TOOLS
)


class MetaService:
    def __init__(
        self,
        session: AsyncSession,
        redis: aioredis.Redis,
        settings: Settings,
    ) -> None:
        self._repo = ModelCatalogRepository(session)
        self._redis = redis
        self._settings = settings

    async def _available_models(self) -> set[str] | None:
        """网关可用模型集合。None 表示巡检失败。"""
        try:
            cached = await self._redis.get(_MODELS_CACHE_KEY)
        except Exception:  # Redis 挂了不该让 /models 挂
            logger.warning("redis unavailable while reading models cache", exc_info=True)
            cached = None

        if cached:
            return set(json.loads(cached))

        cfg = GatewayConfig(
            base_url=str(self._settings.litellm_base_url),
            api_key=self._settings.litellm_key.get_secret_value(),
            timeout_s=self._settings.litellm_timeout_s,
        )
        try:
            models = await list_models(cfg)
        except ModelUnavailable:
            logger.warning("litellm gateway probe failed", exc_info=True)
            return None

        try:
            await self._redis.set(
                _MODELS_CACHE_KEY, json.dumps(models), ex=self._settings.models_cache_ttl_s
            )
        except Exception:
            logger.warning("redis unavailable while writing models cache", exc_info=True)
        return set(models)

    async def list_models(self) -> ModelListResponse:
        available = await self._available_models()
        if available is not None:
            await self._repo.sync_availability(available)

        rows = await self._repo.list_all()
        return ModelListResponse(
            data=[
                ModelInfo(
                    model=r.model,
                    provider=r.provider,
                    display_name=r.display_name,
                    context_window=r.context_window,
                    max_output_tokens=r.max_output_tokens,
                    supports_thinking=r.supports_thinking,
                    supports_adaptive_thinking=r.supports_adaptive_thinking,
                    supports_effort=r.supports_effort,
                    supports_cache=r.supports_cache,
                    supports_temperature=r.supports_temperature,
                    min_cacheable_tokens=r.min_cacheable_tokens,
                    is_available=r.is_available,
                )
                for r in rows
            ],
            gateway_reachable=available is not None,
        )

    async def list_tools(self) -> ToolListResponse:
        tools = list(_BUILTIN_TOOLS)

        if self._settings.serpapi_key is None:
            tools = [
                t.model_copy(update={"available": False, "note": "服务端未配置 SERPAPI_KEY"})
                if t.name == WEB_SEARCH_TOOL
                else t
                for t in tools
            ]

        # P7：并入远程 MCP server 上发现的工具（§11 GET /tools）。
        # 单个 server 不可用不会让整张目录挂掉 —— McpService 内部已容错，
        # 那台的工具不出现在列表里，其余照常。
        mcp = McpService(self._settings.mcp_servers)
        if mcp.configured:
            tools.extend(
                ToolInfo(
                    name=item["name"],
                    kind="mcp",
                    display_name=item["display_name"],
                    description=item["description"],
                    available=True,
                    note=f"来自 MCP server {item['server']}",
                )
                for item in await mcp.list_tools()
            )

        return ToolListResponse(data=tools)
