"""DefinitionSync：MCP 代理的定义同步。

领到期的服务（写租约），在锁外拉一次完整的 tools/list，再锁住服务行重读、按指纹比对：新增的登记、
变了的把旧版留在 previous、上游没了的标删除。比对结果交回 ServiceCatalog，由它发 tools_removed。

★ 没有复核：上游新增、改了的工具拉到就生效；有服务级授权的 Key 自动拿到新工具。
★ 拉取失败保留旧定义并记下原因；手动刷新 10 秒内只能一次。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID

from ..db.models import McpService, McpTool
from ..db.types import utcnow
from ..errors import Invalid, TooSoon
from ..settings import GatewaySettings
from ..upstream import DiscoveryFailed
from ..upstream.gateway import UpstreamGateway
from .definitions import ToolDefinition, content_hash, normalize
from .repository import ServiceRepository

logger = logging.getLogger(__name__)

__all__ = ["DefinitionSync", "tool_from"]


def tool_from(d: ToolDefinition, model: Any = McpTool) -> McpTool:
    """model：McpTool（租户的服务）或 McpPlatformTool（平台服务），列一样。"""
    return model(
        name=d.name,
        description=d.description,
        input_schema=d.input_schema,
        annotations=d.annotations,
        effect=d.effect,
        digest=d.digest,
        issues=list(d.issues),
        status="active",
    )


class DefinitionSync:
    def __init__(
        self, repo: ServiceRepository, upstream: UpstreamGateway, settings: GatewaySettings
    ) -> None:
        self._repo = repo
        self._upstream = upstream
        self._settings = settings

    async def claim_due(self) -> list[tuple[str, UUID]]:
        """领到该刷新的 MCP 代理（多实例只让一个去拉）。→ [(服务名, 租户)]。

        跨租户：调用方在系统范围里调，刷新时逐个进入服务所属的租户。
        """
        now = utcnow()
        return await self._repo.claim_due(
            due=now - timedelta(seconds=self._settings.refresh_interval_s),
            lease=now - timedelta(seconds=max(self._settings.discovery_timeout_s * 4, 60)),
            now=now,
        )

    async def refresh(self, row: McpService, *, manual: bool = False) -> list[str]:
        """从上游拉一次完整的工具列表，存库。→ 这次上游没了的工具。失败保留旧定义、记下原因。"""
        if row.kind != "mcp":
            raise Invalid("只有 MCP 代理的服务从上游拉取工具定义")
        if manual and row.fetched_at is not None:
            since = (utcnow() - row.fetched_at).total_seconds()
            if since < self._settings.manual_refresh_min_interval_s:
                raise TooSoon(
                    f"{self._settings.manual_refresh_min_interval_s:g} 秒内只能刷新一次",
                    service=row.name,
                )
        try:  # ★ 在锁外拉：拉一次完整的工具列表可能要十几秒，不能占着行锁
            found = await self._upstream.discover(row.upstream_spec())
            error = None
        except DiscoveryFailed as exc:  # 拉取失败的细节给管理员看，不往上抛
            found, error = None, str(exc)
        await self._repo.lock(row)
        row.fetched_at = utcnow()
        row.refresh_claimed_at = None
        if found is None:
            logger.warning("MCP 服务 %s 刷新失败，继续使用旧定义：%s", row.name, error)
            row.fetch_error = error
            return []
        row.fetch_error = None
        return await self.apply(row, [normalize(row.name, t) for t in found.tools])

    async def apply(self, svc: McpService, defs: Sequence[ToolDefinition]) -> list[str]:
        """按指纹比对、写定义。→ 上游没了的工具（标 removed_at，留行）。"""
        session = self._repo.session
        rows = {t.name: t for t in await self._repo.tools(svc, include_removed=True)}
        now = utcnow()
        changed: list[str] = []
        removed: list[str] = []
        seen: set[str] = set()
        for d in defs:
            if d.name in seen:
                continue  # 上游给了同名的两个工具：只认第一个
            seen.add(d.name)
            row = rows.get(d.name)
            if row is None:
                row = tool_from(d)
                row.service_uuid = svc.uuid
                rows[d.name] = row
                session.add(row)
                changed.append(d.name)
                continue
            if row.digest != d.digest:
                row.previous = {
                    "description": row.description,
                    "input_schema": row.input_schema,
                    "annotations": row.annotations,
                    "digest": row.digest,
                }
                row.changed_at = now
                changed.append(d.name)
            elif row.removed_at is not None:
                changed.append(d.name)
            row.description, row.input_schema = d.description, d.input_schema
            row.annotations, row.digest, row.effect = d.annotations, d.digest, d.effect
            row.issues = list(d.issues)
            row.removed_at = None
        for name, row in rows.items():
            if name not in seen and row.removed_at is None:
                row.removed_at = now
                changed.append(name)
                removed.append(name)
        digest = content_hash(r.digest for r in rows.values() if r.removed_at is None)
        if digest != svc.content_hash:
            if svc.content_hash is not None:
                logger.info("MCP 服务 %s 的工具定义变了：%s", svc.name, "、".join(changed))
            svc.content_hash = digest
            svc.changed_at = now
        await session.flush()
        return removed
