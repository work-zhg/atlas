"""ServiceRepository：读写 mcp_service、mcp_tool。

★ 领租约是一条 UPDATE … RETURNING；写定义前 SELECT … FOR UPDATE 重读，多副本的刷新串行。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import McpService, McpTool
from ..errors import NotFound

__all__ = ["ServiceRepository"]


class ServiceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self) -> list[McpService]:
        return list(await self.session.scalars(select(McpService).order_by(McpService.name)))

    async def find(self, name: str) -> McpService | None:
        return await self.session.scalar(select(McpService).where(McpService.name == name))

    async def get(self, name: str) -> McpService:
        row = await self.find(name)
        if row is None:
            raise NotFound(f"MCP 服务 {name} 不存在", service=name)
        return row

    async def tools(self, svc: McpService, *, include_removed: bool = False) -> list[McpTool]:
        stmt = select(McpTool).where(McpTool.service_uuid == svc.uuid).order_by(McpTool.name)
        if not include_removed:
            stmt = stmt.where(McpTool.removed_at.is_(None))
        return list(await self.session.scalars(stmt))

    async def tool(self, svc: McpService, name: str) -> McpTool:
        row = await self.session.scalar(
            select(McpTool).where(
                McpTool.service_uuid == svc.uuid, McpTool.name == name, McpTool.removed_at.is_(None)
            )
        )
        if row is None:
            raise NotFound(f"MCP 服务 {svc.name} 上没有工具 {name}", tool=name)
        return row

    async def lock(self, svc: McpService) -> None:
        """锁住服务行并重新读一遍（别的实例可能刚刷新过）。先 flush：重读会覆盖没写下去的改动。"""
        await self.session.flush()
        await self.session.refresh(svc, with_for_update=True)

    async def claim_due(
        self, *, due: datetime, lease: datetime, now: datetime
    ) -> list[tuple[str, UUID]]:
        """领到期的 MCP 代理（写租约时间 = 领取）。→ [(服务名, 租户)]。"""
        result = await self.session.execute(
            update(McpService)
            .where(
                McpService.kind == "mcp",
                McpService.status == "enabled",
                or_(McpService.fetched_at.is_(None), McpService.fetched_at < due),
                or_(McpService.refresh_claimed_at.is_(None), McpService.refresh_claimed_at < lease),
            )
            .values(refresh_claimed_at=now)
            .returning(McpService.name, McpService.tenant_uuid)
        )
        return [(r[0], r[1]) for r in result.all()]
