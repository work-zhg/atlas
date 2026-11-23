"""定时任务的只读管理接口（JobRoutes）：在运营接口里（/ops/v1/jobs），只给运营中心；刷新工具定义是
本实例的实例任务。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from gateway_testkit import ADMIN, TEST_TENANT, as_user, in_tenant, ops_request


async def test_jobs_are_listed_for_managers_only(
    perms: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlas_mcpgateway.api import app as app_mod
    from atlas_mcpgateway.settings import get_settings

    # 测试环境把间隔设成 0（不注册）；这里打开，第一轮在 30 秒后，用例里不会真跑
    settings = get_settings().model_copy(update={"maintainer_tick_s": 30})
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    app = app_mod.create_app(uc=perms)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client,
    ):
        gone = await client.get("/api/v1/jobs", headers=in_tenant(TEST_TENANT, **as_user(ADMIN)))
        denied = await client.get("/ops/v1/jobs", headers=as_user(ADMIN))
        ok = await client.request("GET", "/ops/v1/jobs", **ops_request("GET", "/ops/v1/jobs"))

    assert gone.status_code == 404, "租户里没有了"
    assert denied.status_code == 401
    assert ok.status_code == 200
    [job] = ok.json()["jobs"]
    assert (job["type"], job["scope"], job["every"]) == ("mcpgateway.tool_refresh", "instance", 30)
    assert job["description"]
