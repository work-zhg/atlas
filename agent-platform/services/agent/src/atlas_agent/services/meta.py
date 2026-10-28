"""元数据服务：编辑器里可选的模型、工具、知识库。

模型：经模型管理平台时按智能体的客户端 Key 列出（模型管理方案设计 7.2）；没接平台时
model_catalog ⋈ litellm 巡检结果（文档 §11.4）。取不到时**不报错**，把 gateway_reachable 置 false，
让前端能提示（§13.2「不允许静默降级」）。
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
from atlas_agent.providers.llm.model_platform import (
    ModelPlatformUnavailable,
    PlatformModel,
    agent_key,
    platform_catalog,
    uses_model_platform,
)

from ..config import Settings
from ..errors import CapabilityUnavailable
from ..providers.knowledge import KnowledgeUnavailable, readable_as
from ..providers.mcp import make_mcp_catalog, mcp_subject_access, review_status, tool_id
from ..repositories.model_catalog import ModelCatalogRepository
from ..schemas.meta import (
    KnowledgeBaseInfo,
    KnowledgeListResponse,
    KnowledgeSubjectOut,
    McpSubjectOut,
    ModelInfo,
    ModelKeyOut,
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

    async def list_models(
        self, *, agent_id: UUID | None = None, agent_name: str = ""
    ) -> ModelListResponse:
        """agent_id：编辑已有的智能体；新建时不给。"""
        if uses_model_platform(self._settings):
            return await self._platform_models(agent_id, agent_name)
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

    async def _platform_models(self, agent_id: UUID | None, agent_name: str) -> ModelListResponse:
        """经模型管理平台（模型管理方案设计 7.2）：智能体的客户端 Key 分配了模型就只列它们；
        还没分配（新建、待分配、停用）就先列平台上登记的全部模型，并说明分配之前不能运行。

        ★ 与权限类型无关：同一个智能体对所有使用者能用的模型相同。
        ★ 思考、档位、温度等能力仍按 model_catalog 表（平台不登记能力）；表里没有的按不支持处理。
        """
        out = ModelKeyOut(status="none")
        try:
            key = await agent_key(self._settings, agent_id, agent_name) if agent_id else None
            if key is not None:
                out.status = key.status  # type: ignore[assignment]
            if key is not None and key.status == "active":
                models = list(key.models)
            else:
                models = await platform_catalog(self._settings)
                if key is None:
                    out.notes.append("保存后由模型管理员给这个智能体分配模型，分配之前它不能运行")
                elif (problem := key.problem()) is not None:
                    out.notes.append(f"{problem}（这里先列出平台上登记的全部模型）")
        except ModelPlatformUnavailable as exc:
            logger.warning("模型管理平台不可用，模型列表为空：%s", exc)
            out.notes.append(f"模型管理平台暂时不可用：{exc}")
            return ModelListResponse(data=[], gateway_reachable=False, key=out)
        rows = {r.model: r for r in await self._repo.list_all()}
        return ModelListResponse(
            data=[_platform_info(m, rows.get(m.name)) for m in models],
            gateway_reachable=True,
            key=out,
        )

    async def list_knowledge(self, subject: McpSubject) -> KnowledgeListResponse:
        """编辑器里可选的知识库：权限主体能读的（与运行时、保存时同一道闸门）。

        ★ 以 Agent 为权限、还没有数字员工（第一次保存之前）：一个都不列，提示保存后请库的
          所有者共享给数字员工（同 MCP）。
        """
        out = KnowledgeSubjectOut(kind=subject.kind, uid=subject.uid)
        who = "这个 Agent 的数字员工" if subject.kind == "agent" else "你"
        if subject.uid is None:
            out.notes.append("保存后请库的所有者把知识库共享给这个 Agent 的数字员工，再回来绑定")
            return KnowledgeListResponse(data=[], subject=out)
        try:
            rows = await readable_as(self._settings, subject.uid)
        except KnowledgeUnavailable as exc:
            out.notes.append(f"知识库服务暂时不可用：{exc}")
            return KnowledgeListResponse(data=[], subject=out)
        if rows is None:
            out.notes.append("服务端未接入知识库服务，不能绑定知识库")
            return KnowledgeListResponse(data=[], subject=out)
        if not rows:
            out.notes.append(f"{who}还没有能读的知识库：请库的所有者共享给{who}")
        return KnowledgeListResponse(
            data=[
                KnowledgeBaseInfo(
                    id=str(d["id"]),
                    name=str(d.get("name") or ""),
                    description=d.get("description"),
                    icon=d.get("icon"),
                )
                for d in rows
            ],
            subject=out,
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


def _platform_info(m: PlatformModel, row: Any) -> ModelInfo:
    """平台上的模型 → 编辑器认的形状。规格先看平台（LiteLLM 里登记的），能力看 model_catalog 表。"""
    return ModelInfo(
        model=m.name,
        # 经平台一律走 Anthropic Messages（LiteLLM 转换）
        provider="anthropic",
        display_name=m.display_name,
        context_window=m.max_input_tokens or (row.context_window if row else 0),
        max_output_tokens=m.max_output_tokens or (row.max_output_tokens if row else 0),
        supports_thinking=bool(row and row.supports_thinking),
        supports_adaptive_thinking=bool(row and row.supports_adaptive_thinking),
        supports_effort=bool(row and row.supports_effort),
        supports_cache=bool(row and row.supports_cache),
        supports_temperature=bool(row and row.supports_temperature),
        min_cacheable_tokens=row.min_cacheable_tokens if row else 0,
        is_available=True,
    )
