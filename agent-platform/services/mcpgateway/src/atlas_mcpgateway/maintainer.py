"""维护任务：定期重新拉取 MCP 代理的工具定义。

多实例各跑一个；谁领到（服务目录 claim_due 写租约）谁拉，一个服务同一时间只有一个实例在拉。
定时是定时任务库 atlas_jobs 的实例任务（ToolRefresh）：循环、出错隔离、停机交给库，运行情况在
运营接口 /ops/v1/jobs 看。
"""

from __future__ import annotations

import logging

from atlas_jobs import Job, JobContext, NoParams
from atlas_tenancy import SystemReason, TenantScope

from . import wiring
from .access.usercenter import UserCenter
from .db.session import get_sessionmaker
from .settings import GatewaySettings
from .upstream.gateway import UpstreamGateway

logger = logging.getLogger(__name__)

__all__ = ["ToolRefresh", "refresh_due"]


async def refresh_due(
    settings: GatewaySettings,
    uc: UserCenter,
    upstream: UpstreamGateway,
) -> list[str]:
    """拉一轮到期的服务，返回拉了哪些。每个服务一个事务：一个出错不影响别的。

    ★ 跨租户巡检的固定写法：在系统范围里领（只拿服务名和租户），逐个进入服务所属的租户去拉。
    """
    sessionmaker = get_sessionmaker()
    with TenantScope.system(SystemReason.SWEEP):
        async with sessionmaker() as session:
            due = await wiring.catalog(session, settings, uc, upstream).claim_due()
            await session.commit()
    done: list[str] = []
    for name, tenant in due:
        try:
            with TenantScope.enter(tenant):
                async with sessionmaker() as session:
                    catalog = wiring.catalog(session, settings, uc, upstream)
                    await catalog.refresh(await catalog.get(name))
                    await session.commit()
            done.append(name)
        except Exception:
            logger.exception("MCP 服务 %s 定期刷新失败", name)
    return done


class ToolRefresh(Job):
    type = "mcpgateway.tool_refresh"
    description = "刷新 MCP 工具定义"

    def __init__(
        self,
        settings: GatewaySettings,
        uc: UserCenter,
        upstream: UpstreamGateway,
    ) -> None:
        self._settings, self._uc, self._upstream = settings, uc, upstream

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        await refresh_due(self._settings, self._uc, self._upstream)
