"""定时任务的只读管理接口（JobRoutes）：只给 model:manage；LiteLLM 对账是本实例的实例任务。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from modelgw_testkit import ADMIN


async def test_jobs_are_listed_for_managers_only(
    perms: Any, litellm: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlas_modelgateway.api import app as app_mod
    from atlas_modelgateway.settings import get_settings

    # 测试环境把间隔设成 0（不注册）；这里打开，第一轮在 5 分钟后，用例里不会真跑
    settings = get_settings().model_copy(update={"reconcile_interval_s": 300})
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    app = app_mod.create_app(perms=perms, litellm_transport=litellm.transport())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://platform"
        ) as client,
    ):
        denied = await client.get("/api/v1/jobs", headers={"X-Uid": "someone"})
        ok = await client.get("/api/v1/jobs", headers={"X-Uid": ADMIN})

    assert denied.status_code == 403
    assert ok.status_code == 200
    [job] = ok.json()["jobs"]
    assert (job["type"], job["scope"], job["every"]) == ("modelgateway.reconcile", "instance", 300)
    assert job["description"]
