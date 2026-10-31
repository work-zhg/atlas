"""维护任务：定期重新拉取 MCP 代理的工具定义。

多实例各跑一个；谁领到（registry.claim_due 写租约）谁拉，一个服务同一时间只有一个实例在拉。
"""

from __future__ import annotations

import asyncio
import logging

from .db.session import get_sessionmaker
from .perms import Permissions
from .registry import Registry
from .settings import GatewaySettings
from .upstreams.hub import Upstreams

logger = logging.getLogger(__name__)

__all__ = ["refresh_due", "run_maintainer"]


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


async def run_maintainer(
    settings: GatewaySettings, perms: Permissions, upstreams: Upstreams
) -> None:
    while True:
        try:
            await refresh_due(settings, perms, upstreams)
        except Exception:
            logger.exception("MCP 网关维护任务出错，下一轮再试")
        await asyncio.sleep(settings.maintainer_tick_s)
