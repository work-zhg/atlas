"""实例任务的行为测试（c4-model/infra-platform/code-jobs.html 的 InstanceRunner）。

不碰数据库：实例任务不抢行、不写执行记录。间隔取几十毫秒，用例在一秒内跑完。
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from atlas_jobs import Job, JobContext, JobScheduler, NoParams
from pydantic import BaseModel


class Counting(Job):
    type = "test.counting"

    def __init__(self) -> None:
        self.calls: list[JobContext] = []

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        self.calls.append(ctx)


async def _until(ok, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not ok():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("等不到期望的状态")
        await asyncio.sleep(0.01)


async def test_runs_every_interval_until_stopped() -> None:
    job = Counting()
    jobs = JobScheduler()
    jobs.register(job, scope="instance", every=0.02)
    jobs.start()
    await _until(lambda: len(job.calls) >= 3)
    await jobs.stop()
    stopped_at = len(job.calls)
    await asyncio.sleep(0.08)

    assert len(job.calls) == stopped_at, "stop 之后还在跑"
    assert all(c.job_name == "test.counting" and c.still_held() for c in job.calls)
    [metrics] = jobs.metrics()
    assert metrics.runs == stopped_at and metrics.failures == 0
    assert metrics.last_started_at is not None and metrics.last_duration_s is not None


async def test_a_failing_round_does_not_stop_the_loop() -> None:
    class Flaky(Job):
        type = "test.flaky"

        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, ctx: JobContext, params: NoParams) -> None:
            self.calls += 1
            if self.calls <= 2:
                raise RuntimeError(f"第 {self.calls} 轮失败")

    job = Flaky()
    jobs = JobScheduler()
    jobs.register(job, scope="instance", every=0.02)
    jobs.start()
    await _until(lambda: jobs.metrics()[0].consecutive_failures == 2)
    assert "第 2 轮失败" in (jobs.metrics()[0].last_error or "")
    await _until(lambda: job.calls >= 4)
    await jobs.stop()

    [metrics] = jobs.metrics()
    assert metrics.failures == 2
    assert metrics.consecutive_failures == 0 and metrics.last_error is None, "成功一次就该清零"


async def test_a_slow_round_is_not_overlapped() -> None:
    """一轮比间隔还久：下一轮等它跑完，不会叠上来。"""

    class Slow(Job):
        type = "test.slow"

        def __init__(self) -> None:
            self.running = 0
            self.peak = 0
            self.calls = 0

        async def execute(self, ctx: JobContext, params: NoParams) -> None:
            self.running += 1
            self.peak = max(self.peak, self.running)
            await asyncio.sleep(0.05)
            self.running -= 1
            self.calls += 1

    job = Slow()
    jobs = JobScheduler()
    jobs.register(job, scope="instance", every=0.01)
    jobs.start()
    await _until(lambda: job.calls >= 3)
    await jobs.stop()
    assert job.peak == 1


async def test_a_sync_job_runs_off_the_event_loop_thread() -> None:
    class Blocking(Job):
        type = "test.blocking"

        def __init__(self) -> None:
            self.threads: list[str] = []

        def execute(self, ctx: JobContext, params: NoParams) -> None:
            self.threads.append(threading.current_thread().name)

    job = Blocking()
    jobs = JobScheduler()
    jobs.register(job, scope="instance", every=0.02)
    jobs.start()
    await _until(lambda: len(job.threads) >= 2)
    await jobs.stop()
    assert all(name.startswith("atlas-jobs") for name in job.threads), job.threads


async def test_execute_gets_the_declared_params_with_defaults() -> None:
    class PageParams(BaseModel):
        page: int = 10

    class WithParams(Job):
        type = "test.params"
        Params = PageParams

        def __init__(self) -> None:
            self.seen: list[int] = []

        async def execute(self, ctx: JobContext, params: PageParams) -> None:
            self.seen.append(params.page)

    job = WithParams()
    jobs = JobScheduler()
    jobs.register(job, scope="instance", every=0.02)
    jobs.start()
    await _until(lambda: bool(job.seen))
    await jobs.stop()
    assert job.seen[0] == 10


def test_registration_is_checked() -> None:
    jobs = JobScheduler()
    jobs.register(Counting(), scope="instance", every=10, jitter=3)
    with pytest.raises(ValueError, match="重复"):
        jobs.register(Counting(), scope="instance", every=10)
    with pytest.raises(ValueError, match="抖动"):
        JobScheduler().register(Counting(), scope="instance", every=1, jitter=1)
    with pytest.raises(ValueError, match="间隔"):
        JobScheduler().register(Counting(), scope="instance", every=0)

    class Nameless(Job):
        async def execute(self, ctx: JobContext, params: NoParams) -> None: ...

    with pytest.raises(ValueError, match="type"):
        JobScheduler().register(Nameless(), scope="instance", every=1)


async def test_cannot_register_after_start() -> None:
    jobs = JobScheduler()
    jobs.register(Counting(), scope="instance", every=10)
    jobs.start()
    try:
        with pytest.raises(RuntimeError):
            jobs.register(Counting(), scope="instance", every=10)
    finally:
        await jobs.stop()
