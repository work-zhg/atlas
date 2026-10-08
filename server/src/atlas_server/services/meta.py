"""元数据服务：model_catalog ⋈ 网关巡检结果（文档 §11.4）。

网关不可达时**不报错** —— 退化为 catalog 中的存量 is_available，
并在响应里把 gateway_reachable 置 false，让前端能提示"可用性未刷新"。
这是 §13.2「不允许静默降级」的应用。
"""

from __future__ import annotations

import asyncio
import json
import logging

import redis.asyncio as aioredis
from atlas_engine.contracts import ModelUnavailable
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_server.domain.tool_registry import BUILTIN_TOOLS
from atlas_server.providers.llm.gateway import GatewayConfig, list_models

from ..config import Settings
from ..configplane import ConfigPlaneUnavailable
from ..configplane.mcp import make_mcp_catalog, mcp_reviews
from ..providers.mcp import review_status, tool_id
from ..repositories.model_catalog import ModelCatalogRepository
from ..schemas.meta import ModelInfo, ModelListResponse, ToolInfo, ToolListResponse

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
        tools.extend(await self._mcp_tools())
        return ToolListResponse(data=tools)

    async def _mcp_tools(self) -> list[ToolInfo]:
        """远程 MCP server 上的工具，读目录缓存（MCP 详设 §05）。

        单个 server 不可用不会让整张目录挂掉：有缓存就照常列出（note 里带上
        最近一次刷新的错误），冷缓存又连不上的那台不出现，其余照常。
        """
        try:
            catalog = await make_mcp_catalog(self._settings, self._redis)
            reviews = await mcp_reviews(self._settings, catalog.servers())
        except ConfigPlaneUnavailable as exc:
            # 注册表拉不到：内置工具照常列出，MCP 那部分缺席（并说明原因）
            logger.warning("MCP 注册表不可用，工具目录里暂缺 MCP 工具：%s", exc)
            return []
        names = [c.name for c in catalog.servers()]
        results = await asyncio.gather(
            *(catalog.snapshot(n) for n in names), return_exceptions=True
        )
        out: list[ToolInfo] = []
        for name, snap in zip(names, results, strict=True):
            if isinstance(snap, BaseException):
                logger.warning("MCP server %s 不可用：%s", name, snap)
                continue
            config = catalog.server(name)
            for d in snap.tools:
                status = review_status(config, d, reviews) if config else "invalid"
                notes = [f"来自 MCP server {name}", *d.issues]
                if status == "pending_review":
                    notes.append("定义未经复核，暂不可用")
                elif status == "rejected":
                    notes.append("定义复核未通过")
                if snap.error:
                    notes.append(f"最近一次刷新失败：{snap.error}")
                out.append(
                    ToolInfo(
                        name=tool_id(name, d.name),
                        kind="mcp",
                        display_name=d.name,
                        description=d.description[:500],
                        available=status == "ok",
                        note="；".join(notes),
                        # ★ require_approval_for 匹配的是模型侧名 —— 此前这里
                        #   为空，编辑器里根本选不到 MCP 工具做审批
                        model_tool_names=[d.model_name] if d.model_name else [],
                        server=name,
                        digest=d.digest,
                        review_status=status,
                    )
                )
        return out
