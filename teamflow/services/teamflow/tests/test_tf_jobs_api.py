"""定时任务的只读管理接口（JobRoutes）：只给 settings:manage（平台管理员）；协作者对账、扫描待处理
节点是本实例的实例任务。扫描本身的行为见 test_tf_multi_instance.py。"""

from __future__ import annotations

from tf_testkit import World, ok


async def test_jobs_are_listed_for_platform_admins_only() -> None:
    w = World()
    padmin = await w.as_("padmin")
    dev = await w.as_("dev")
    jobs = w.app.state.jobs
    jobs.start()  # lifespan 里做的那一步（测试的 ASGITransport 不跑 lifespan）
    try:
        denied = await dev.get("/api/v1/jobs")
        body = ok(await padmin.get("/api/v1/jobs"))
    finally:
        await jobs.stop()

    assert denied.status_code == 403
    assert {j["type"]: j["every"] for j in body["jobs"]} == {
        "teamflow.collaborator_reconcile": w.settings.collaborator_reconcile_seconds,
        "teamflow.node_sweep": w.settings.worker_sweep_seconds,
    }
