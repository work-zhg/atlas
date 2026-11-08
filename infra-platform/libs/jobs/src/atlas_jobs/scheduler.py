"""库的入口（c4-model/infra-platform/code-jobs.html 的 JobScheduler）。"""

from __future__ import annotations

from typing import Literal

from .instance import InstanceMetrics, InstanceRunner
from .job import Job
from .registry import JobRegistry


class JobScheduler:
    """注册任务、起停调度。

    ★ 停机由宿主调 stop()，时机由宿主决定：比如 Run 执行器要先交接完，交接期间续租这个
      实例任务还得一直跑，所以宿主在自己的收尾做完之后再调。不该在停机时继续的任务
      （比如兜底），在 execute 开头自己判断「停机中」就返回。
    """

    def __init__(self) -> None:
        self._registry = JobRegistry()
        self._instance: InstanceRunner | None = None

    def register(
        self,
        job: Job,
        *,
        scope: Literal["instance"],
        every: float,
        jitter: float = 0.0,
    ) -> None:
        if self._instance is not None:
            raise RuntimeError("调度已经启动，不能再注册任务")
        self._registry.register(job, scope=scope, every=every, jitter=jitter)

    def start(self) -> None:
        """起本进程的全部实例任务。要在宿主的事件循环里调用。"""
        if self._instance is None:
            self._instance = InstanceRunner(self._registry.instance_jobs())
        self._instance.start()

    async def stop(self) -> None:
        if self._instance is not None:
            await self._instance.stop()

    def metrics(self) -> list[InstanceMetrics]:
        """每个实例任务最近一次的开始、结束、耗时、连续失败次数。还没启动时是空的。"""
        return self._instance.metrics() if self._instance is not None else []
