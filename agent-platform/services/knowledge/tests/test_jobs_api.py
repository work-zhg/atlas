"""定时任务的只读管理接口（JobRoutes）：只给 job:manage（Atlas 应用的操作码）；入库巡检是本实例的
实例任务。巡检本身的行为见 test_ingest_recovery.py。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest


async def test_jobs_are_listed_for_job_admins_only(
    rt: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlas_knowledge import auth
    from atlas_knowledge.api.app import create_app

    def handler(req: httpx.Request) -> httpx.Response:
        uid = req.url.path.split("/")[-2]
        ops = ["kb:create", "job:manage"] if uid == "admin" else ["kb:create", "kb:review"]
        return httpx.Response(
            200, json={"can_access": True, "permissions": ops, "user": {"status": "active"}}
        )

    monkeypatch.setattr(auth, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(auth, "_cache", auth._Cache())
    rt.settings = rt.settings.model_copy(update={"ingest_sweep_interval_s": 30})
    app = create_app(runtime=rt)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://kb") as c,
    ):
        denied = await c.get("/v1/jobs", headers={"X-Uid": "alice"})
        ok = await c.get("/v1/jobs", headers={"X-Uid": "admin"})

    assert denied.status_code == 403
    assert ok.status_code == 200
    [job] = ok.json()["jobs"]
    assert (job["type"], job["description"], job["every"]) == (
        "knowledge.ingest_sweep",
        "入库巡检",
        30,
    )
