"""CatalogEventHandler：授权对目录事件的实现（服务目录的扩展点 CatalogEvents）。

    service_registered   在用户中心登记 McpServer，登记人成为所有者
    service_renamed      同步改名
    tools_removed        删掉 McpTool 对象和它的工具级授权

★ 同一个事务里同步分发：这里失败，目录的操作整个回滚。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import McpService
from ..errors import Conflict
from .objects import DataObjectRegistry
from .usercenter import DATA_SERVER, DATA_TOOL, UserCenter, tool_data_id

logger = logging.getLogger(__name__)

__all__ = ["CatalogEventHandler"]


class CatalogEventHandler:
    def __init__(self, session: AsyncSession, uc: UserCenter) -> None:
        self._uc = uc
        self._objects = DataObjectRegistry(session)

    async def service_registered(self, service: McpService, owner: str) -> None:
        name = service.display_name or service.name
        try:
            await self._uc.register(DATA_SERVER, service.name, name, owners=[owner])
        except Conflict:
            # ★ 网关库里没有这个服务，用户中心里却有：旧版本或登记到一半失败留下的。
            #   网关库是服务是否存在的事实源，清掉重来
            logger.warning("用户中心里有没人管的 McpServer %s，清掉后重新登记", service.name)
            await self._uc.drop(DATA_SERVER, service.name)
            await self._uc.register(DATA_SERVER, service.name, name, owners=[owner])
        await self._objects.add(DATA_SERVER, service.name, service.uuid)

    async def service_renamed(self, service: McpService) -> None:
        await self._uc.rename(DATA_SERVER, service.name, service.display_name)

    async def tools_removed(self, service: McpService, names: Sequence[str]) -> None:
        for name in names:
            row = await self._objects.find(DATA_TOOL, tool_data_id(service.name, name))
            if row is not None:  # 只有授过工具级权限的才登记过
                await self._uc.drop(DATA_TOOL, row.data_id)
                await self._objects.remove(row)
