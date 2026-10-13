"""MCP 注册表与工具复核（设计 §8）。

★ 本服务**不连** MCP server：凭据只在运行时进程里，这里连不上需要认证的 server，
  也不该连。工具发现、digest 计算、变更检测都在运行时；这里只存「定义」与
  「人对某个 digest 的结论」。
★ 没有删除：server 名是工具前缀，被 agent_version.spec 引用着；删了再建同名
  server 会让历史配置指向一个不同的东西。停用即可。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..db.models import McpServer, McpToolReview
from ..db.types import utcnow
from ..errors import Conflict, Invalid, NotFound
from ..schemas import McpServerIn, McpServerOut, McpServerPatch, ToolReviewIn, ToolReviewOut

__all__ = ["McpRegistry"]


def _out(row: McpServer) -> McpServerOut:
    return McpServerOut(
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        transport=row.transport,
        url=row.url,
        headers=dict(row.headers or {}),
        credential_scope=row.credential_scope,
        call_timeout_s=row.call_timeout_s,
        review_required=row.review_required,
        enabled=row.status == "enabled",
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


@dataclass
class McpRegistry:
    session: AsyncSession

    async def list(self, *, enabled_only: bool = False) -> list[McpServerOut]:
        query = select(McpServer).order_by(McpServer.name)
        if enabled_only:
            query = query.where(McpServer.status == "enabled")
        return [_out(r) for r in (await self.session.scalars(query)).all()]

    async def get(self, name: str) -> McpServerOut:
        return _out(await self._row(name))

    async def create(self, data: McpServerIn, *, actor: str) -> McpServerOut:
        if await self.session.get(McpServer, data.name) is not None:
            raise Conflict(f"MCP server {data.name} 已存在（名字是工具前缀，不能复用）")
        row = McpServer(
            name=data.name,
            display_name=data.display_name,
            description=data.description,
            transport=data.transport,
            url=data.url,
            headers=data.headers,
            credential_scope=data.credential_scope,
            call_timeout_s=data.call_timeout_s,
            review_required=data.review_required,
            status=data.status,
            created_by=actor,
            updated_by=actor,
        )
        self.session.add(row)
        audit.record(
            self.session,
            actor=actor,
            action="mcp.create",
            target_kind="mcp_server",
            target_id=data.name,
            url=data.url,
            header_names=sorted(data.headers),
        )
        await self.session.flush()
        return _out(row)

    async def patch(self, name: str, data: McpServerPatch, *, actor: str) -> McpServerOut:
        row = await self._row(name, lock=True)
        changes = data.model_dump(exclude_unset=True)
        for key, value in changes.items():
            setattr(row, key, value)
        row.updated_by = actor
        row.updated_at = utcnow()
        audit.record(
            self.session,
            actor=actor,
            action="mcp.update",
            target_kind="mcp_server",
            target_id=name,
            # ★ 只记 header 名，不记值 —— 值虽然只是占位符，审计里也不必有
            changed={k: (sorted(v) if k == "headers" else v) for k, v in changes.items()},
        )
        await self.session.flush()
        return _out(row)

    async def reviews(self, server: str) -> list[ToolReviewOut]:
        rows = (
            await self.session.scalars(
                select(McpToolReview)
                .where(McpToolReview.server == server)
                .order_by(McpToolReview.tool_name, McpToolReview.decided_at)
            )
        ).all()
        return [ToolReviewOut.model_validate(r, from_attributes=True) for r in rows]

    async def review(self, server: str, data: ToolReviewIn, *, actor: str) -> ToolReviewOut:
        await self._row(server)
        row = await self.session.get(McpToolReview, (server, data.tool_name, data.digest))
        if row is None:
            row = McpToolReview(server=server, tool_name=data.tool_name, digest=data.digest)
            self.session.add(row)
        row.decision = data.decision
        row.note = data.note
        row.decided_by = actor
        row.decided_at = utcnow()
        audit.record(
            self.session,
            actor=actor,
            action=f"mcp.tool_{data.decision}",
            target_kind="mcp_server",
            target_id=server,
            tool=data.tool_name,
            digest=data.digest,
            note=data.note or None,
        )
        await self.session.flush()
        return ToolReviewOut.model_validate(row, from_attributes=True)

    async def import_env(self, raw: str, *, actor: str) -> list[str]:
        """从运行时的 MCP_SERVERS（JSON 数组）一次性导入。已存在的跳过。

        ★ 导入的 server 默认 review_required=False：它们在环境变量里已经跑了一段
          时间，一上来全部「待复核」等于把现有 agent 的 MCP 工具全部关掉。
          要收紧时逐个打开。
        """
        try:
            items: list[dict[str, Any]] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise Invalid(f"MCP_SERVERS 不是合法 JSON：{exc}") from exc
        created: list[str] = []
        for item in items:
            try:
                data = McpServerIn(
                    name=item["name"],
                    url=item["url"],
                    transport=item.get("transport", "streamable_http"),
                    headers=item.get("headers") or {},
                    call_timeout_s=item.get("call_timeout_s"),
                    status="enabled" if item.get("enabled", True) else "disabled",
                    review_required=False,
                )
            except (KeyError, ValidationError) as exc:
                raise Invalid(f"MCP_SERVERS 里的 {item.get('name')!r} 不合规：{exc}") from exc
            if await self.session.get(McpServer, data.name) is not None:
                continue
            await self.create(data, actor=actor)
            created.append(data.name)
        return created

    async def _row(self, name: str, *, lock: bool = False) -> McpServer:
        query = select(McpServer).where(McpServer.name == name)
        if lock:
            query = query.with_for_update()
        row = await self.session.scalar(query)
        if row is None:
            raise NotFound(f"MCP server {name} 不存在")
        return row
