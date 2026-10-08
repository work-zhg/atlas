"""技能 / MCP 的只读视图（技能 / MCP 设计 §13.3）。

管理页要的东西分在两边：内容与审查在配置服务，「谁在用」「用得怎样」「MCP 此刻
的工具定义」在运行时。这里提供后一半 —— 配置服务不反向调运行时。
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from atlas_config.schemas import SENSITIVE_HEADER
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..configplane import ConfigPlaneUnavailable, skill_directory
from ..configplane.mcp import make_mcp_catalog, mcp_reviews
from ..db.models import Agent, AgentVersion, Thread
from ..domain.mcp_naming import parse_tool_id, tool_id
from ..errors import AppError, DependencyUnavailable, NotFound
from ..providers.mcp import McpUnavailable, review_status
from ..providers.mcp.catalog import McpToolDef, ServerSnapshot, describe_error
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


def _public_url(url: str) -> str:
    """去掉 userinfo 与 query —— 两处都可能夹带凭据。"""
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _public_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: (v if "${" in v or not SENSITIVE_HEADER.match(k) else "••••") for k, v in headers.items()
    }


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
        except ConfigPlaneUnavailable as exc:
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
        reviews = await self._reviews(configs)
        refs = Counter(server for server, _ in await self._mcp_refs())
        out = []
        for config in configs:
            snap = await catalog.cached(config.name) if config.enabled else None
            out.append(self._summary(config, snap, reviews, refs.get(config.name, 0)))
        return McpServerListOut(registry=self._settings.mcp_registry, data=out)

    async def mcp_server(self, name: str) -> McpServerDetailOut:
        configs, catalog = await self._mcp_configs()
        config = next((c for c in configs if c.name == name), None)
        if config is None:
            raise NotFound(f"MCP server {name} 不存在", server=name)
        reviews = await self._reviews([config])
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
            detail.removed_tools = sorted(n for n in previous if n not in current)
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
        except McpUnavailable as exc:
            raise DependencyUnavailable(str(exc), server=name) from exc
        except Exception as exc:  # 发现失败的细节给管理员看，不是 500
            raise DependencyUnavailable(describe_error(exc), server=name) from exc
        return await self.mcp_server(name)

    # ================================================================ 内部

    async def _mcp_configs(self) -> tuple[list[McpServerConfig], Any]:
        try:
            catalog = await make_mcp_catalog(self._settings, self._redis)
        except ConfigPlaneUnavailable as exc:
            raise DependencyUnavailable(f"MCP 注册表暂时不可用：{exc}") from exc
        if self._settings.mcp_registry == "env":
            # 列表里也要看见被停用的 server（catalog 只认 enabled 的）
            return list(self._settings.mcp_servers), catalog
        return catalog.servers(), catalog

    async def _reviews(self, configs: list[McpServerConfig]) -> Any:
        try:
            return await mcp_reviews(self._settings, configs)
        except ConfigPlaneUnavailable:
            return {}

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
            url=_public_url(config.url),
            transport=config.transport,
            credential_scope=config.credential_scope,
            headers=_public_headers(config.headers),
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
