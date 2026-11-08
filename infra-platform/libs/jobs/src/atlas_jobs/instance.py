"""实例任务的调度（c4-model/infra-platform/code-jobs.html 的 InstanceRunner）。

每个实例任务一个协程，跑在宿主的事件循环上：按登记的间隔加随机抖动触发；上一轮没跑完，
下一轮不会叠上来；出错只记日志、算进失败次数，循环不退出。不抢行、不要租约、不写执行记录，
只更新指标。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from .job import JobContext
from .registry import InstanceJob

logger = logging.getLogger(__name__)


@dataclass
class InstanceMetrics:
    """一个实例任务在本进程里的运行情况，给管理接口和监控读。"""

    type: str
    every: float
    runs: int = 0
    failures: int = 0
    #: 连续失败次数：成功一次就清零。一直涨说明这个任务在本进程里已经不起作用了
    consecutive_failures: int = 0
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_duration_s: float | None = None
    last_error: str | None = None


class InstanceRunner:
    def __init__(self, jobs: list[InstanceJob], *, rng: random.Random | None = None) -> None:
        self._jobs = jobs
        self._rng = rng or random.Random()
        self._metrics = {j.job.type: InstanceMetrics(type=j.job.type, every=j.every) for j in jobs}
        self._loops: list[asyncio.Task[None]] = []
        # ★ 同步任务进库自己的线程池，不用 AnyIO 的默认线程池：那 40 个线程是给宿主的同步接口
        #   用的。实例任务各自不重叠，一个同步任务最多占一个线程
        sync = sum(1 for j in jobs if not inspect.iscoroutinefunction(j.job.execute))
        self._pool = (
            ThreadPoolExecutor(max_workers=sync, thread_name_prefix="atlas-jobs") if sync else None
        )

    def start(self) -> None:
        if self._loops:
            return
        for entry in self._jobs:
            # ★ 全新的 contextvars.Context：宿主启动时的上下文（请求身份之类）不该带进任务
            self._loops.append(
                asyncio.create_task(
                    self._loop(entry),
                    name=f"job:{entry.job.type}",
                    context=contextvars.Context(),
                )
            )

    async def stop(self) -> None:
        for task in self._loops:
            task.cancel()
        for task in self._loops:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._loops.clear()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def metrics(self) -> list[InstanceMetrics]:
        return [replace(m) for m in self._metrics.values()]

    def _delay(self, entry: InstanceJob) -> float:
        return entry.every + self._rng.uniform(-entry.jitter, entry.jitter)

    async def _loop(self, entry: InstanceJob) -> None:
        # ★ 一轮跑完才睡下一段：慢的时候不会越积越多（上一轮没跑完，下一轮跳过）
        while True:
            await asyncio.sleep(self._delay(entry))
            await self._run_once(entry)

    async def _run_once(self, entry: InstanceJob) -> None:
        job = entry.job
        metrics = self._metrics[job.type]
        fired_at = datetime.now(UTC)
        started = time.monotonic()
        metrics.last_started_at = fired_at
        ctx = JobContext(job_name=job.type, fired_at=fired_at)
        try:
            params = job.Params()
            if inspect.iscoroutinefunction(job.execute):
                await job.execute(ctx, params)  # type: ignore[misc]
            else:
                await asyncio.get_running_loop().run_in_executor(
                    self._pool, job.execute, ctx, params
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            metrics.failures += 1
            metrics.consecutive_failures += 1
            metrics.last_error = repr(exc)[:500]
            logger.warning("实例任务 %s 这一轮失败", job.type, exc_info=True)
        else:
            metrics.consecutive_failures = 0
            metrics.last_error = None
        finally:
            metrics.runs += 1
            metrics.last_finished_at = datetime.now(UTC)
            metrics.last_duration_s = time.monotonic() - started
