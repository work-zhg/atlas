"""定时任务（atlas_jobs 的实例任务，每个副本各跑一份）。

teamflow.dispatch_sweep   扫描排着的指令、要取消的 run（补漏掉的唤醒、接手挂掉的副本没投完的）
teamflow.notify           按流程事件写站内通知、推渠道（游标行锁，同时只有一个副本在推）
"""

from __future__ import annotations

from atlas_jobs import Job, JobContext, NoParams

from .collab.dispatcher import InstructionDispatcher
from .inbox.notifier import Notifier

__all__ = ["DispatchSweep", "Notify"]


class DispatchSweep(Job):
    type = "teamflow.dispatch_sweep"
    description = "扫描排着的指令、要取消的 run"

    def __init__(self, dispatcher: InstructionDispatcher) -> None:
        self._dispatcher = dispatcher

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        await self._dispatcher.sweep()


class Notify(Job):
    type = "teamflow.notify"
    description = "按流程事件生成通知"

    def __init__(self, notifier: Notifier) -> None:
        self._notifier = notifier

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        while await self._notifier.run() >= self._notifier.batch:
            pass
