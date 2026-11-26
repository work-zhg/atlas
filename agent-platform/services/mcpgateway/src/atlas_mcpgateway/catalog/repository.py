"""ServiceRepository：读写 mcp_service、mcp_tool，平台服务读写 mcp_platform_*。

    租户的   ServiceRepository(session)：list 只有本租户的；find、get 先在本租户找，没有再找平台服务
            （名字全平台保留，不会撞）
    平台的   ServiceRepository(session, platform=True)：只认平台服务（运维命令行登记）

★ 领租约是一条 UPDATE … RETURNING；写定义前 SELECT … FOR UPDATE 重读，多副本的刷新串行。
★ 平台表不归租户：在哪个租户里（或没有租户）都查得到，不需要进系统范围。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import McpPlatformService, McpPlatformTool, McpService, McpTool
from ..errors import NotFound

__all__ = ["AnyService", "AnyTool", "ServiceRepository"]

#: 租户的服务或平台服务（列和行为一样，见 db/models.py 的 _ServiceColumns）
AnyService = McpService | McpPlatformService
AnyTool = McpTool | McpPlatformTool


class ServiceRepository:
    def __init__(self, session: AsyncSession, *, platform: bool = False) -> None:
        self.session = session
        self.platform = platform
        #: 这个仓储新建服务、工具时用的类
        self.service_model: Any = McpPlatformService if platform else McpService
        self.tool_model: Any = McpPlatformTool if platform else McpTool

    async def list(self) -> list[AnyService]:
        """本租户的服务（平台仓储：全部平台服务）。不含平台服务：要的话另调 platform_services。"""
        model = self.service_model
        return list(await self.session.scalars(select(model).order_by(model.name)))

    async def platform_services(self) -> list[McpPlatformService]:
        return list(
            await self.session.scalars(select(McpPlatformService).order_by(McpPlatformService.name))
        )

    async def find(self, name: str) -> AnyService | None:
        row = await self.session.scalar(
            select(self.service_model).where(self.service_model.name == name)
        )
        if row is None and not self.platform:
            row = await self.find_platform(name)
        return row  # type: ignore[no-any-return]

    async def find_platform(self, name: str) -> McpPlatformService | None:
        return await self.session.scalar(
            select(McpPlatformService).where(McpPlatformService.name == name)
        )

    async def get(self, name: str) -> AnyService:
        row = await self.find(name)
        if row is None:
            raise NotFound(f"MCP 服务 {name} 不存在", service=name)
        return row

    def _tool_model(self, svc: AnyService) -> Any:
        return McpPlatformTool if svc.platform else McpTool

    async def tools(self, svc: AnyService, *, include_removed: bool = False) -> list[AnyTool]:
        model = self._tool_model(svc)
        stmt = select(model).where(model.service_uuid == svc.uuid).order_by(model.name)
        if not include_removed:
            stmt = stmt.where(model.removed_at.is_(None))
        return list(await self.session.scalars(stmt))

    async def tool(self, svc: AnyService, name: str) -> AnyTool:
        model = self._tool_model(svc)
        row = await self.session.scalar(
            select(model).where(
                model.service_uuid == svc.uuid, model.name == name, model.removed_at.is_(None)
            )
        )
        if row is None:
            raise NotFound(f"MCP 服务 {svc.name} 上没有工具 {name}", tool=name)
        return row  # type: ignore[no-any-return]

    async def lock(self, svc: AnyService) -> None:
        """锁住服务行并重新读一遍（别的实例可能刚刷新过）。先 flush：重读会覆盖没写下去的改动。"""
        await self.session.flush()
        await self.session.refresh(svc, with_for_update=True)

    async def claim_due(
        self, *, due: datetime, lease: datetime, now: datetime
    ) -> list[tuple[str, UUID]]:
        """领到期的 MCP 代理（写租约时间 = 领取）。→ [(服务名, 租户)]。

        ★ 只领租户的：平台服务只能登记 REST、gRPC 转换（工具在登记时定好），没有要拉的定义。
        """
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
