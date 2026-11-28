"""待办与通知；运营接口的租户删除。"""

from __future__ import annotations

from atlas_teamflow.api.ops import TeamFlowTenantLifecycle
from atlas_tenancy import TenantScope
from kit import World, ok, setup_project


async def test_review_opened_notifies_reviewers_once() -> None:
    w = World()
    env = await setup_project(w)
    zhou = w.as_("zhou")
    proc = ok(
        await zhou.post(
            f"/api/v1/projects/{env['project']['id']}/processes",
            {"template_id": env["template"]["id"], "title": "支付回调幂等"},
        ),
        201,
    )
    pid = proc["id"]
    ok(await zhou.post(f"/api/v1/processes/{pid}/nodes/clarify/begin"))
    ok(await zhou.put(f"/api/v1/processes/{pid}/space?path=需求澄清/记录.md", content=b"x"), 201)
    ok(
        await zhou.post(
            f"/api/v1/processes/{pid}/nodes/clarify/submissions", {"paths": ["需求澄清/记录.md"]}
        ),
        201,
    )
    notifier = w.app.state.notifier
    assert await notifier.run() > 0
    await notifier.run()  # 再跑一轮不会多发
    got = ok(await zhou.get("/api/v1/notifications"))
    texts = [n["text"] for n in got["items"]]
    assert any("等你准出评审" in t for t in texts)
    assert sum("等你准出评审" in t for t in texts) == 1
    assert got["unread"] == len(texts)
    ok(await zhou.post("/api/v1/notifications/read", {}))
    assert ok(await zhou.get("/api/v1/notifications"))["unread"] == 0
    # 别的租户的人看不到
    assert ok(await w.as_("lin").get("/api/v1/notifications"))["items"] == []


async def test_tenant_delete_releases_spaces_and_purges() -> None:
    w = World()
    env = await setup_project(w)
    proc = ok(
        await w.as_("zhou").post(
            f"/api/v1/projects/{env['project']['id']}/processes",
            {"template_id": env["template"]["id"], "title": "支付回调幂等"},
        ),
        201,
    )
    lifecycle = TeamFlowTenantLifecycle(w.app)
    report = await lifecycle.delete(w.tenant)
    while report.remaining:
        report = await lifecycle.delete(w.tenant)
    assert ("release_workspace", proc["space_id"]) in w.atlas.calls
    with TenantScope.enter(w.tenant):
        r = await w.as_("li").get("/api/v1/teams")
    assert ok(r) == []
