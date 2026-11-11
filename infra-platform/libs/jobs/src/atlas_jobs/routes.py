"""管理接口：宿主挂到自己的 FastAPI 应用上（JobRoutes(jobs, guard).router）。

    GET   /jobs        本进程的实例任务：周期、正在不在跑、下一轮、运行与失败次数、最近一轮

★ 这一版只读：实例任务定义在代码里，没有可改的东西；新建、暂停、立即触发、执行历史等集群任务
  实现后再加。
★ 实例任务每个进程各跑一份、指标在进程内存里，所以返回的是**回应这个请求的那个实例**的情况，
  响应里带上实例标识（默认主机名，K8s 里就是 Pod 名）；进程重启后计数清零。
★ 每个请求先经 JobGuard 鉴权。库不认识用户中心和操作码。
"""

from __future__ import annotations

import socket
from datetime import datetime
from typing import Literal, Protocol

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from .instance import InstanceMetrics
from .scheduler import JobScheduler

__all__ = ["JobGuard", "JobRoutes"]


class JobGuard(Protocol):
    """谁能看定时任务：返回操作人，没权限就抛 HTTPException(403)。"""

    async def view(self, request: Request) -> str: ...


class InstanceJobOut(BaseModel):
    type: str
    description: str
    scope: Literal["instance"] = Field(description="instance = 每个实例各跑一份")
    every: float = Field(description="两轮之间隔多久（秒）")
    jitter: float = Field(description="每轮在 every 上随机加减的秒数")
    running: bool
    next_fire_at: datetime | None = Field(description="下一轮大约什么时候开始；正在跑时为空")
    runs: int
    failures: int
    consecutive_failures: int = Field(description="连续失败次数，成功一次就清零")
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_duration_s: float | None
    last_error: str | None


class JobsOut(BaseModel):
    instance: str = Field(description="回应这个请求的实例：指标只是它的")
    jobs: list[InstanceJobOut]


def _out(m: InstanceMetrics) -> InstanceJobOut:
    return InstanceJobOut(
        type=m.type,
        description=m.description,
        scope="instance",
        every=m.every,
        jitter=m.jitter,
        running=m.running,
        next_fire_at=m.next_fire_at,
        runs=m.runs,
        failures=m.failures,
        consecutive_failures=m.consecutive_failures,
        last_started_at=m.last_started_at,
        last_finished_at=m.last_finished_at,
        last_duration_s=m.last_duration_s,
        last_error=m.last_error,
    )


class JobRoutes:
    def __init__(
        self,
        jobs: JobScheduler,
        guard: JobGuard,
        *,
        prefix: str = "/jobs",
        instance: str | None = None,
    ) -> None:
        self._jobs = jobs
        self._guard = guard
        self._instance = instance or socket.gethostname()
        self.router = APIRouter(prefix=prefix, tags=["jobs"])
        self.router.add_api_route("", self.list, methods=["GET"], response_model=JobsOut)

    async def list(self, request: Request) -> JobsOut:
        await self._guard.view(request)
        return JobsOut(
            instance=self._instance,
            jobs=sorted((_out(m) for m in self._jobs.metrics()), key=lambda j: j.type),
        )
