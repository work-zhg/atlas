"""定时任务的只读管理接口（JobRoutes）：在运营接口里（/ops/v1/jobs），只给运营中心；入库巡检是本实例
的实例任务。巡检本身的行为见 test_ingest_recovery.py。"""

from __future__ import annotations

from typing import Any

import httpx
from conftest import ops_request


async def test_jobs_are_listed_for_the_ops_center_only(rt: Any) -> None:
    from atlas_knowledge.api.app import create_app

    rt.settings = rt.settings.model_copy(update={"ingest_sweep_interval_s": 30})
    app = create_app(runtime=rt)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://kb") as c,
    ):
        gone = await c.get("/v1/jobs", headers={"X-Uid": "admin"})
        denied = await c.get("/ops/v1/jobs", headers={"X-Uid": "admin"})
        ok = await c.request("GET", "/ops/v1/jobs", **ops_request("GET", "/ops/v1/jobs"))

    assert gone.status_code == 404, "租户里没有了"
    assert denied.status_code == 401
    assert ok.status_code == 200
    jobs = {j["type"]: j for j in ok.json()["jobs"]}
    assert set(jobs) == {"knowledge.ingest_sweep", "knowledge.enhance_cache_cleanup"}
    job = jobs["knowledge.ingest_sweep"]
    assert (job["description"], job["every"]) == ("入库巡检（含索引版本重建）", 30)
    assert jobs["knowledge.enhance_cache_cleanup"]["every"] == 24 * 3600
