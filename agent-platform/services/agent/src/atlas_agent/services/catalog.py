"""技能 / MCP 的只读视图（技能 / MCP 设计 §13.3）。

管理页要的东西分在几处：技能的内容与审查在技能应用；MCP 的注册表、工具定义、复核、授权在
MCP 网关（doc/mcp-design.html 第二版）；「谁在用」「用得怎样」只有运行时知道。这里提供最后
这一半 —— 技能应用、MCP 网关都不反向调运行时。MCP 的工具定义读网关的元数据（缓存）。
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..db.models import Agent, AgentVersion, Thread
from ..domain.mcp_naming import parse_tool_id, tool_id
from ..errors import AppError, CapabilityUnavailable, DependencyUnavailable, NotFound
from ..providers.mcp import GatewayCatalog, make_mcp_catalog, review_status
from ..providers.mcp.catalog import McpToolDef, ServerSnapshot
from ..providers.mcp.config import McpServerConfig
from ..repositories.skill_usage import SkillUsageRepository
from ..schemas.catalog import (
    McpServerDetailOut,
    McpServerListOut,
    McpServerSummary,
    McpToolOut,
    SkillListItem,
    SkillListOut,
    SkillReference,
    SkillReferencesOut,
    SkillUsageItem,
    SkillUsageOut,
)
from ..skills import SkillServiceUnavailable, skill_directory

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from ..config import Settings

__all__ = ["CatalogService", "RefreshTooSoon"]

#: 定义变化后多久内列表显示 changed
_CHANGED_WINDOW_S = 7 * 24 * 3600
_REFRESH_LOCK = "atlas:mcp:manual-refresh:{}"
_REFRESH_EVERY_S = 10


class RefreshTooSoon(AppError):
    status_code = 429
    kind = "refresh_too_soon"


def _today() -> date:
    return datetime.now(UTC).date()


def _ts(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value else None


class CatalogService:
    def __init__(
        self, session: AsyncSession, settings: Settings, redis: aioredis.Redis | None
    ) -> None:
        self._session = session
        self._settings = settings
        self._redis = redis

    # ================================================================ 技能

    async def list_skills(self) -> SkillListOut:
        directory = skill_directory(self._settings)
        try:
            catalog = await directory.catalog()
        except SkillServiceUnavailable as exc:
            raise DependencyUnavailable(f"技能目录暂时不可用：{exc}") from exc
        refs = Counter(r.skill for r in await self._skill_refs())
        loads: Counter[str] = Counter()
        for row in await SkillUsageRepository(self._session).summary(days=7, today=_today()):
            loads[row["slug"]] += row["loads"]
        return SkillListOut(
            directory_configured=directory.configured,
            data=[
                SkillListItem(
                    slug=item.slug,
                    source=item.source,
                    latest=item.latest,
                    description=item.description,
                    has_scripts=item.has_scripts,
                    versions=item.versions,
                    references=refs.get(item.slug, 0),
                    loads_7d=loads.get(item.slug, 0),
                )
                for item in catalog
            ],
        )

    async def skill_references(self, slug: str) -> SkillReferencesOut:
        return SkillReferencesOut(data=[r.out for r in await self._skill_refs() if r.skill == slug])

    async def skill_usage(self, days: int) -> SkillUsageOut:
        rows = await SkillUsageRepository(self._session).summary(days=days, today=_today())
        return SkillUsageOut(days=days, data=[SkillUsageItem(**r) for r in rows])

    # ================================================================ MCP

    async def list_mcp_servers(self) -> McpServerListOut:
        configs, catalog = await self._mcp_configs()
        reviews = self._reviews(catalog)
        refs = Counter(server for server, _ in await self._mcp_refs())
        out = []
        for config in configs:
            snap = await catalog.cached(config.name) if config.enabled else None
            out.append(self._summary(config, snap, reviews, refs.get(config.name, 0)))
        return McpServerListOut(registry="gateway", data=out)

    async def mcp_server(self, name: str) -> McpServerDetailOut:
        configs, catalog = await self._mcp_configs()
        config = next((c for c in configs if c.name == name), None)
        if config is None:
            raise NotFound(f"MCP server {name} 不存在", server=name)
        reviews = self._reviews(catalog)
        snap = await catalog.cached(name) if config.enabled else None
        refs = [r for r in await self._mcp_refs() if r[0] == name]
        detail = McpServerDetailOut(
            **self._summary(config, snap, reviews, len(refs)).model_dump(),
            referenced_by=[ref for _, ref in refs],
        )
        if snap is not None:
            previous = {t.name: t for t in snap.previous}
            detail.tools = [self._tool(config, t, previous, reviews) for t in snap.tools]
            current = {t.name for t in snap.tools}
            detail.removed_tools = sorted(
                {n for n in previous if n not in current} | set(snap.removed)
            )
        return detail

    async def refresh_mcp_server(self, name: str) -> McpServerDetailOut:
        configs, catalog = await self._mcp_configs()
        if not any(c.name == name and c.enabled for c in configs):
            raise NotFound(f"MCP server {name} 不存在或已停用", server=name)
        if self._redis is not None and not await self._redis.set(
            _REFRESH_LOCK.format(name), "1", nx=True, ex=_REFRESH_EVERY_S
        ):
            raise RefreshTooSoon(f"{_REFRESH_EVERY_S} 秒内只能刷新一次", server=name)
        try:
            await catalog.refresh(name)
        except CapabilityUnavailable as exc:
            raise DependencyUnavailable(str(exc), server=name) from exc
        return await self.mcp_server(name)

    # ================================================================ 内部

    async def _mcp_configs(self) -> tuple[list[McpServerConfig], GatewayCatalog]:
        """MCP 网关上的服务（含停用的：列表里也要看见它们）。"""
        try:
            catalog = await make_mcp_catalog(self._settings, self._redis)
        except CapabilityUnavailable as exc:
            raise DependencyUnavailable(f"MCP 网关暂时不可用：{exc}") from exc
        return catalog.all_servers(), catalog

    @staticmethod
    def _reviews(catalog: GatewayCatalog) -> Any:
        return catalog.reviews()

    def _summary(
        self,
        config: McpServerConfig,
        snap: ServerSnapshot | None,
        reviews: Any,
        references: int,
    ) -> McpServerSummary:
        statuses = [review_status(config, t, reviews) for t in snap.tools] if snap else []
        if not config.enabled:
            status, error = "disabled", None
        elif snap is None:
            status, error = "error", "还没有成功发现过工具 —— 点刷新试一次"
        elif snap.error:
            status, error = "stale", snap.error
        elif snap.changed_at and time.time() - snap.changed_at < _CHANGED_WINDOW_S:
            status, error = "changed", None
        else:
            status, error = "ok", None
        return McpServerSummary(
            name=config.name,
            url=config.url,
            transport="streamable_http",
            # 经 MCP 网关、带权限主体的 API Key（MCP 方案设计 §5.6）
            credential_scope="subject",
            headers={},
            call_timeout_s=config.call_timeout_s,
            enabled=config.enabled,
            review_required=config.review_required,
            status=status,
            error=error,
            tools_count=len(snap.tools) if snap else 0,
            invalid_tools=statuses.count("invalid"),
            pending_review=statuses.count("pending_review"),
            content_hash=snap.content_hash if snap else None,
            fetched_at=_ts(snap.fetched_at) if snap else None,
            changed_at=_ts(snap.changed_at) if snap else None,
            references=references,
        )

    @staticmethod
    def _tool(
        config: McpServerConfig,
        tool: McpToolDef,
        previous: dict[str, McpToolDef],
        reviews: Any,
    ) -> McpToolOut:
        change = None
        before = previous.get(tool.name)
        if previous and before is None:
            change = "added"
        elif before is not None and before.digest != tool.digest:
            change = "description" if before.description != tool.description else "schema"
        return McpToolOut(
            name=tool.name,
            id=tool_id(config.name, tool.name),
            model_name=tool.model_name,
            description=tool.description,
            digest=tool.digest,
            issues=list(tool.issues),
            review_status=review_status(config, tool, reviews),
            change=change,
            previous_description=before.description if change == "description" and before else None,
        )

    async def _current_specs(self) -> list[tuple[Agent, AgentVersion]]:
        cv = aliased(AgentVersion)
        rows = await self._session.execute(
            select(Agent, cv)
            .join(cv, cv.id == Agent.current_version_id)
            .where(Agent.status != "archived")
            .order_by(Agent.slug)
        )
        return [(a, v) for a, v in rows.all()]

    async def _active_threads(self) -> dict[UUID, int]:
        rows = await self._session.execute(
            select(Thread.agent_id, func.count())
            .where(Thread.status == "active", Thread.parent_thread_id.is_(None))
            .group_by(Thread.agent_id)
        )
        return {agent_id: int(n) for agent_id, n in rows.all()}

    async def _skill_refs(self) -> list[_SkillRef]:
        """按 agent **当前版本**反查（设计 §13.4）。百级 agent 全量遍历即可。"""
        threads = await self._active_threads()
        out: list[_SkillRef] = []
        for agent, version in await self._current_specs():
            spec = version.spec or {}
            owners = [(None, spec)] + [
                (sub.get("name"), sub)
                for sub in spec.get("subagents") or []
                if isinstance(sub, dict)
            ]
            for sub_name, part in owners:
                for ref in part.get("skills") or []:
                    if not isinstance(ref, dict) or not ref.get("slug"):
                        continue
                    out.append(
                        _SkillRef(
                            skill=ref["slug"],
                            out=SkillReference(
                                agent_id=agent.id,
                                agent_slug=agent.slug,
                                agent_name=agent.name,
                                agent_version=version.version,
                                subagent=sub_name,
                                skill_version=int(ref.get("version") or 0),
                                active_threads=threads.get(agent.id, 0),
                            ),
                        )
                    )
        return out

    async def _mcp_refs(self) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        for agent, version in await self._current_specs():
            spec = version.spec or {}
            owners = [(None, spec)] + [
                (sub.get("name"), sub)
                for sub in spec.get("subagents") or []
                if isinstance(sub, dict)
            ]
            for sub_name, part in owners:
                tools_by_server: dict[str, list[str]] = {}
                for name in part.get("tool_names") or []:
                    if parsed := parse_tool_id(str(name)):
                        tools_by_server.setdefault(parsed[0], []).append(parsed[1])
                for server, tools in tools_by_server.items():
                    out.append(
                        (
                            server,
                            {
                                "agent_id": str(agent.id),
                                "agent_slug": agent.slug,
                                "agent_name": agent.name,
                                "agent_version": version.version,
                                "subagent": sub_name,
                                "tools": sorted(tools),
                            },
                        )
                    )
        return out


class _SkillRef:
    __slots__ = ("out", "skill")

    def __init__(self, skill: str, out: SkillReference) -> None:
        self.skill = skill
        self.out = out
