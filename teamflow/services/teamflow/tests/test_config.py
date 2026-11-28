"""配置这一侧：团队、数字员工、流程市场、流程模板、项目与配齐。"""

from __future__ import annotations

from atlas_tenancy import TenantScope
from kit import World, ok, setup_project, structure


async def test_team_create_needs_team_create_operation() -> None:
    w = World()
    r = await w.as_("zhou").post("/api/v1/teams", {"name": "研发一部"})
    assert r.status_code == 403
    team = ok(await w.as_("li").post("/api/v1/teams", {"name": "研发一部"}), 201)
    assert team["my_level"] == "OWNER"
    # 李工在用户中心是这个团队的 Owner
    with TenantScope.enter(w.tenant):
        assert (await w.uc.data_level("Team", team["id"], w.ids["li"])) == "OWNER"
    # 同名团队
    assert (await w.as_("li").post("/api/v1/teams", {"name": "研发一部"})).status_code == 409


async def test_team_visibility_and_levels() -> None:
    w = World()
    li = w.as_("li")
    team = ok(
        await li.post(
            "/api/v1/teams", {"name": "研发一部", "members": [{"id": str(w.ids["zhou"])}]}
        ),
        201,
    )
    assert [t["name"] for t in ok(await w.as_("zhou").get("/api/v1/teams"))] == ["研发一部"]
    assert ok(await w.as_("chen").get("/api/v1/teams")) == []
    assert (await w.as_("chen").get(f"/api/v1/teams/{team['id']}")).status_code == 404
    # 读写成员不能改团队配置
    r = await w.as_("zhou").patch(f"/api/v1/teams/{team['id']}", {"name": "改名"})
    assert r.status_code == 403
    members = ok(await li.get(f"/api/v1/teams/{team['id']}/members"))
    assert {m["name"]: m["level"] for m in members["members"]} == {"李工": "OWNER", "周工": "WRITE"}


async def test_only_owner_can_bring_a_digital_employee() -> None:
    w = World()
    li = w.as_("li")
    team = ok(await li.post("/api/v1/teams", {"name": "研发一部"}), 201)
    mine = w.atlas.add_agent("开发工程师", w.ids["li"])
    personal = w.atlas.add_agent("我的助理", w.ids["li"], auth_subject="user")
    others = w.atlas.add_agent("别人的", w.ids["zhou"])
    w.atlas.agents[others]["grants"].append(
        {"id": "g", "subject": {"type": "USER", "id": str(w.ids["li"])}, "level": "READ"}
    )
    r = await li.post(f"/api/v1/teams/{team['id']}/agents", {"atlas_agent_id": personal})
    assert r.status_code == 422 and r.json()["code"] == "NOT_DIGITAL_EMPLOYEE"
    r = await li.post(f"/api/v1/teams/{team['id']}/agents", {"atlas_agent_id": others})
    assert r.status_code == 422 and r.json()["code"] == "AGENT_OWNER_REQUIRED"
    a = ok(await li.post(f"/api/v1/teams/{team['id']}/agents", {"atlas_agent_id": mine}), 201)
    assert a["added_by"] == str(w.ids["li"])
    candidates = ok(await li.get(f"/api/v1/teams/{team['id']}/agents/candidates"))
    # 以人为权限的不在候选里
    assert {c["name"]: c["added"] for c in candidates} == {"开发工程师": True, "别人的": False}


async def test_market_maintained_by_system_tenant_and_copied_by_teams() -> None:
    w = World()
    lin, li = w.as_("lin"), w.as_("li")
    # 普通租户的人不能维护市场
    assert (
        await li.post("/api/v1/market", {"name": "软件研发", "category": "rd"})
    ).status_code == 403
    m = ok(await lin.post("/api/v1/market", {"name": "软件研发", "category": "rd"}), 201)
    # 草稿有错误不能发布
    assert (await lin.post(f"/api/v1/market/{m['id']}/publish", {})).status_code == 422
    ok(
        await lin.put(
            f"/api/v1/market/{m['id']}/draft", {"structure": structure(), "description": "标准研发"}
        )
    )
    ok(await lin.post(f"/api/v1/market/{m['id']}/publish", {"note": "v1"}), 201)
    listing = ok(await li.get("/api/v1/market"))
    assert listing["maintainer"] is False
    assert [x["name"] for x in listing["items"]] == ["软件研发"]
    team = ok(await li.post("/api/v1/teams", {"name": "研发一部"}), 201)
    t = ok(
        await li.post(f"/api/v1/teams/{team['id']}/templates", {"market_template_id": m["id"]}), 201
    )
    assert t["origin"]["version"] == 1
    assert t["draft"]["structure"]["roles"] == {"pm": "产品", "dev": "开发", "qa": "测试"}
    # 下架后不能再拷；已经拷走的不受影响
    ok(await lin.post(f"/api/v1/market/{m['id']}/listing", {"listed": False}))
    r = await li.post(f"/api/v1/teams/{team['id']}/templates", {"market_template_id": m["id"]})
    assert r.status_code == 409
    detail = ok(await lin.get(f"/api/v1/market/{m['id']}"))
    assert (detail["copies"], detail["tenants"]) == (1, 1)


async def test_template_draft_conflict_and_publish() -> None:
    w = World()
    li = w.as_("li")
    team = ok(await li.post("/api/v1/teams", {"name": "研发一部"}), 201)
    t = ok(await li.post(f"/api/v1/teams/{team['id']}/templates", {"name": "模板"}), 201)
    rev = t["draft"]["rev"]
    ok(await li.put(f"/api/v1/templates/{t['id']}/draft", {"structure": structure(), "rev": rev}))
    # 拿旧的 rev 再存 → 冲突
    r = await li.put(f"/api/v1/templates/{t['id']}/draft", {"structure": structure(), "rev": rev})
    assert r.status_code == 409
    v = ok(await li.post(f"/api/v1/templates/{t['id']}/publish", {"note": "首版"}), 201)
    assert v["version_no"] == 1


async def test_project_readiness_and_launchable() -> None:
    w = World()
    env = await setup_project(w)
    li = w.as_("li")
    pid, tid = env["project"]["id"], env["template"]["id"]
    detail = ok(await li.get(f"/api/v1/projects/{pid}"))
    assert detail["bindings"][0]["ready"] is True
    assert [t["id"] for t in ok(await li.get(f"/api/v1/projects/{pid}/launchable"))] == [tid]
    # 测试角色没有数字员工 → 还差
    ok(
        await li.put(
            f"/api/v1/projects/{pid}/templates/{tid}/roles/qa", {"members": [str(w.ids["chen"])]}
        )
    )
    detail = ok(await li.get(f"/api/v1/projects/{pid}"))
    assert detail["bindings"][0]["missing"] == [{"role": "qa", "what": "no_agent"}]
    assert ok(await li.get(f"/api/v1/projects/{pid}/launchable")) == []
    # 只读成员不能担任角色
    r = await li.put(
        f"/api/v1/projects/{pid}/templates/{tid}/roles/qa", {"members": [str(w.ids["he"])]}
    )
    assert r.status_code == 422


async def test_assigning_roles_grants_use_of_digital_employees() -> None:
    w = World()
    env = await setup_project(w)
    dev_agent = env["agents"]["dev"]["atlas_agent_id"]
    # 流程里的人（周工、陈运、李工）都能用开发工程师
    assert {str(w.ids[x]) for x in ("li", "zhou", "chen")} <= w.atlas.users_with_use(dev_agent)
    assert str(w.ids["he"]) not in w.atlas.users_with_use(dev_agent)
