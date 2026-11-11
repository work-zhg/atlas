"""定时任务的只读管理接口接进 Atlas（c4-model/infra-platform/code-jobs.html · JobRoutes）。

只给 job:manage；返回本实例的实例任务，实例标识与 run 租约的 owner 一致。
库本身的行为（间隔、不重叠、指标）见 infra-platform/libs/jobs/tests。
"""

from __future__ import annotations

import asyncio

import httpx
from atlas_agent.executor.scheduling import LeaseRenewer, RecoveryJob
from atlas_agent.instance import instance_id
from atlas_agent.main import create_app
from atlas_jobs import Job, JobContext, NoParams
from httpx import ASGITransport

from tests.fake_uc import FakeUC


class Tick(Job):
    type = "test.tick"
    description = "测试用"

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        self.calls += 1


async def test_jobs_are_listed_for_job_admins_only(fake_user_center: FakeUC) -> None:
    member = {"X-Uid": fake_user_center.add("member", ["chat:use", "config:manage"])}
    admin = {"X-Uid": fake_user_center.add("admin", ["chat:use", "job:manage"])}
    app = create_app()
    tick = Tick()
    app.state.jobs.register(tick, scope="instance", every=0.02)  # lifespan 里注册执行器的任务
    app.state.jobs.start()
    try:
        for _ in range(200):
            if tick.calls:
                break
            await asyncio.sleep(0.01)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            denied = await c.get("/v1/jobs", headers=member)
            ok = await c.get("/v1/jobs", headers=admin)
    finally:
        await app.state.jobs.stop()

    assert denied.status_code == 403
    assert ok.status_code == 200
    body = ok.json()
    assert body["instance"] == instance_id()
    [item] = body["jobs"]
    assert (item["type"], item["description"], item["scope"]) == ("test.tick", "测试用", "instance")
    assert item["runs"] >= 1


def test_executor_jobs_have_names() -> None:
    assert RecoveryJob.description and LeaseRenewer.description
