"""维护任务：定期重新拉取 MCP 代理的工具定义。

多实例各跑一个；谁领到（registry.claim_due 写租约）谁拉，一个服务同一时间只有一个实例在拉。
定时是定时任务库 atlas_jobs 的实例任务（ToolRefresh）：循环、出错隔离、停机交给库，运行情况在
/api/v1/jobs 看。
"""

from __future__ import annotations

import logging

from atlas_jobs import Job, JobContext, NoParams

from .db.session import get_sessionmaker
from .perms import Permissions
from .registry import Registry
from .settings import GatewaySettings
from .upstreams.hub import Upstreams

logger = logging.getLogger(__name__)

__all__ = ["ToolRefresh", "refresh_due"]


async def refresh_due(
    settings: GatewaySettings, perms: Permissions, upstreams: Upstreams
) -> list[str]:
    """拉一轮到期的服务，返回拉了哪些。每个服务一个事务：一个出错不影响别的。"""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        names = await Registry(session, settings, perms, upstreams).claim_due()
        await session.commit()
    for name in names:
        try:
            async with sessionmaker() as session:
                registry = Registry(session, settings, perms, upstreams)
                await registry.refresh(await registry.get(name))
                await session.commit()
        except Exception:
            logger.exception("MCP 服务 %s 定期刷新失败", name)
    return names


class ToolRefresh(Job):
    type = "mcpgateway.tool_refresh"
    description = "刷新 MCP 工具定义"

    def __init__(self, settings: GatewaySettings, perms: Permissions, upstreams: Upstreams) -> None:
        self._settings, self._perms, self._upstreams = settings, perms, upstreams

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        await refresh_due(self._settings, self._perms, self._upstreams)
