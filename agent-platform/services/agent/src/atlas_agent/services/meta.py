"""元数据服务：model_catalog ⋈ 网关巡检结果（文档 §11.4）。

网关不可达时**不报错** —— 退化为 catalog 中的存量 is_available，
并在响应里把 gateway_reachable 置 false，让前端能提示"可用性未刷新"。
这是 §13.2「不允许静默降级」的应用。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import redis.asyncio as aioredis
from atlas_engine.contracts import ModelUnavailable
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_agent.domain.tool_registry import BASH_TOOL, BUILTIN_TOOLS, MEMORY_TOOL
from atlas_agent.providers.llm.gateway import GatewayConfig, list_models

from ..config import Settings
from ..errors import CapabilityUnavailable
from ..providers.mcp import make_mcp_catalog, mcp_subject_access, review_status, tool_id
from ..repositories.model_catalog import ModelCatalogRepository
from ..schemas.meta import (
    McpSubjectOut,
    ModelInfo,
    ModelListResponse,
    ToolInfo,
    ToolListResponse,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpSubject:
    """编辑器按谁的权限列 MCP 工具（MCP 方案设计 §5.2）。

    uid 为空 = 以 Agent 为权限、还没有数字员工。
    """

    kind: Literal["user", "agent"]
    uid: UUID | None


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
        model_tool_names=list(t.model_names()),
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

    async def list_tools(self, *, mcp_subject: McpSubject | None = None) -> ToolListResponse:
        # Shell / 记忆是否可用取决于部署是否开启了沙箱、接入了记忆服务 —— 目录不能说谎
        off = set()
        if not self._settings.sandbox_enabled:
            off.add(BASH_TOOL)
        if not self._settings.memory_base_url:
            off.add(MEMORY_TOOL)
        tools = [
            t.model_copy(update={"available": False}) if t.name in off else t
            for t in _BUILTIN_TOOLS
        ]
        if mcp_subject is None or not self._settings.mcp_gateway_url:
            return ToolListResponse(data=tools)
        mcp, subject_out = await self._mcp_tools_for(mcp_subject)
        tools.extend(mcp)
        return ToolListResponse(data=tools, mcp_subject=subject_out)

    async def _mcp_tools_for(self, subject: McpSubject) -> tuple[list[ToolInfo], McpSubjectOut]:
        """只列权限主体能用的服务里的工具；没权限的服务只给个数（MCP 方案设计 §9.2）。

        ★ 按主体问网关：编辑器里看到的和运行时装配的是同一个闸门（§5.4）。
        """
        out = McpSubjectOut(kind=subject.kind, uid=subject.uid)
        try:
            catalog = await make_mcp_catalog(self._settings, self._redis)
            reviews = catalog.reviews()
        except CapabilityUnavailable as exc:
            logger.warning("MCP 网关不可用，工具目录里暂缺 MCP 工具：%s", exc)
            out.notes.append(f"MCP 网关暂时不可用：{exc}")
            return [], out
        enabled = [c.name for c in catalog.servers()]
        if subject.uid is None:
            out.hidden_servers = len(enabled)
            out.notes.append(
                "保存后在 MCP 管理端给这个 Agent 的数字员工发放 API Key、授权 MCP 服务"
            )
            return [], out
        try:
            access = await mcp_subject_access(self._settings, subject.uid)
        except CapabilityUnavailable as exc:
            out.notes.append(f"MCP 网关暂时不可用：{exc}")
            return [], out
        out.key_status = access.key_status
        out.hidden_servers = sum(1 for name in enabled if not access.allows(name))
        if access.key is None:
            out.notes.append("还没有可用的 API Key：可以先选，运行时会跳过，并在对话里提示")
        allowed = [name for name in enabled if access.allows(name)]
        tools: list[ToolInfo] = []
        for name in allowed:
            config = catalog.server(name)
            snap = await catalog.snapshot(name)
            assert config is not None
            tools.extend(self._tool_infos(name, config, snap, reviews))
        return tools, out

    @staticmethod
    def _tool_infos(name: str, config: Any, snap: Any, reviews: Any) -> list[ToolInfo]:
        out: list[ToolInfo] = []
        for d in snap.tools:
            status = review_status(config, d, reviews)
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
                    model_tool_names=[d.model_name] if d.model_name else [],
                    server=name,
                    digest=d.digest,
                    review_status=status,
                )
            )
        return out

