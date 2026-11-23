"""DataObjectRegistry：读写 mcp_data_object —— 在用户中心登记过哪些数据对象。

授权据此知道某个工具要先登记还是直接授权、增删所有者时要同步哪些对象，不用逐个问用户中心。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import McpDataObject

__all__ = ["DataObjectRegistry"]


class DataObjectRegistry:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def of_service(self, service_uuid: UUID) -> list[McpDataObject]:
        return list(
            await self._s.scalars(
                select(McpDataObject)
                .where(McpDataObject.service_uuid == service_uuid)
                .order_by(McpDataObject.data_code, McpDataObject.data_id)
            )
        )

    async def find(self, data_code: str, data_id: str) -> McpDataObject | None:
        return await self._s.scalar(
            select(McpDataObject).where(
                McpDataObject.data_code == data_code, McpDataObject.data_id == data_id
            )
        )

    async def add(self, data_code: str, data_id: str, service_uuid: UUID) -> McpDataObject:
        row = await self.find(data_code, data_id)
        if row is None:
            row = McpDataObject(data_code=data_code, data_id=data_id, service_uuid=service_uuid)
            self._s.add(row)
            await self._s.flush()
        return row

    async def remove(self, row: McpDataObject) -> None:
        await self._s.delete(row)
        await self._s.flush()
