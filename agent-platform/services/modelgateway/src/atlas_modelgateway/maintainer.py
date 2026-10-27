"""维护任务：补写暂存的用量记录、到期下线弃用的模型、额度预警（§5.3、§9.2、§10.3）。

多副本各跑一个：补写只管本副本暂存的；到期下线是幂等的（下线过的不再动）；预警各自打日志。
"""

from __future__ import annotations

import asyncio
import logging

from .db.session import get_sessionmaker
from .registry import Registry
from .settings import GatewaySettings
from .usage import Recorder

logger = logging.getLogger(__name__)

__all__ = ["run_maintainer", "tick"]


async def tick(settings: GatewaySettings, recorder: Recorder) -> None:
    await recorder.flush()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        registry = Registry(session, settings)
        retired = await registry.retire_due()
        await session.commit()
        for name in retired:
            logger.warning("模型 %s 到了计划下线时间，已下线", name)
        for budget in await registry.budgets():
            if not budget.active:
                continue
            spent, _ = await registry.spent(budget)
            remaining = budget.amount - spent
            if budget.amount and float(remaining / budget.amount) <= budget.warn_ratio:
                logger.warning(
                    "额度「%s」（%s）估算剩余 %s %s，低于 %.0f%%",
                    budget.name,
                    budget.provider,
                    remaining,
                    budget.currency,
                    budget.warn_ratio * 100,
                )


async def run_maintainer(settings: GatewaySettings, recorder: Recorder) -> None:
    while True:
        try:
            await tick(settings, recorder)
        except Exception:
            logger.exception("模型网关维护任务出错，下一轮再试")
        await asyncio.sleep(settings.maintainer_tick_s)
