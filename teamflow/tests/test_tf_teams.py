"""团队、成员（用户中心 Team 数据权限）、Agent、项目与角色分配。"""

from __future__ import annotations

from tf_testkit import World, ok, published_template


async def _team(w: World) -> tuple[str, dict]:  # type: ignore[type-arg]
    admin = await w.as_("padmin")
    t = ok(
        await admin.post(
            "/api/v1/teams",
            {
                "name": "交易中台",
                "admins": [w.id("lead")],
                "members": [{"type": "USER", "id": w.id("dev")}],
            },
        ),
        201,
    )
    return t["id"], t


async def test_create_team_and_visibility() -> None:
    w = World()
    lead = await w.as_("lead")
    r = await lead.post("/api/v1/teams", {"name": "x", "admins": [w.id("lead")]})
    assert r.status_code == 403  # 只有平台管理员能新建团队
    tid, t = await _team(w)
    assert t["warnings"] == []
    # 指定没有团队管理员角色的人为管理员 → 提示
    admin = await w.as_("padmin")
    t2 = ok(await admin.post("/api/v1/teams", {"name": "支付", "admins": [w.id("dev")]}), 201)
    assert t2["warnings"]

    assert [x["name"] for x in ok(await lead.get("/api/v1/teams"))] == ["交易中台"]
    assert ok(await lead.get("/api/v1/teams"))[0]["my_level"] == "OWNER"
    dev = await w.as_("dev")
    assert {x["name"] for x in ok(await dev.get("/api/v1/teams"))} == {"交易中台", "支付"}
    outsider = await w.as_("outsider")
    assert ok(await outsider.get("/api/v1/teams")) == []
    assert (await outsider.get(f"/api/v1/teams/{tid}")).status_code == 404
    assert len(ok(await admin.get("/api/v1/teams"))) == 2  # team:manage_all 看全部


async def test_members_and_permissions() -> None:
    w = World()
    tid, _ = await _team(w)
    lead = await w.as_("lead")
    dev = await w.as_("dev")
    admin = await w.as_("padmin")

    detail = ok(await lead.get(f"/api/v1/teams/{tid}"))
    assert detail["can"] == {"edit": True, "member": True, "agent": True, "create": True}
    assert ok(await dev.get(f"/api/v1/teams/{tid}"))["can"]["member"] is False

    # 成员不能加人；团队管理员可以
    body = {"subjects": [{"type": "USER", "id": w.id("qa")}]}
    assert (await dev.post(f"/api/v1/teams/{tid}/members", body)).status_code == 403
    ok(await lead.post(f"/api/v1/teams/{tid}/members", body))
    holders = {
        h["user"]["account"] for h in ok(await lead.get(f"/api/v1/teams/{tid}/members"))["holders"]
    }
    assert holders == {"lead", "dev", "qa"}

    # 平台管理员：可调整管理员（team:manage_all），但不能编辑团队
    m = ok(await admin.get(f"/api/v1/teams/{tid}/members"))
    qa_grant = next(g for g in m["grants"] if g["subject"]["id"] == w.id("qa"))
    ok(await admin.patch(f"/api/v1/teams/{tid}/members/{qa_grant['id']}", {"admin": True}))
    assert (await admin.patch(f"/api/v1/teams/{tid}", {"name": "新名"})).status_code == 403

    # 团队改名同步到用户中心
    ok(await lead.patch(f"/api/v1/teams/{tid}", {"name": "交易平台"}))
    assert w.uc.names[tid] == "交易平台"

    # 不能移除最后一个 Owner（用户中心兜底）
    lead_grant = next(g for g in m["grants"] if g["subject"]["id"] == w.id("lead"))
    ok(await lead.patch(f"/api/v1/teams/{tid}/members/{qa_grant['id']}", {"admin": False}))
    r = await lead.delete(f"/api/v1/teams/{tid}/members/{lead_grant['id']}")
    assert r.status_code == 409


async def test_agents_projects_and_roles() -> None:
    w = World()
    tid, _ = await _team(w)
    admin = await w.as_("padmin")
    lead = await w.as_("lead")
    dev = await w.as_("dev")
    tpl = await published_template(admin)

    # Agent 接入
    a1 = w.atlas.add("架构 Agent")
    a2 = w.atlas.add("开发 Agent")
    w.atlas.add("草稿 Agent", status="draft")
    cands = ok(await lead.get(f"/api/v1/teams/{tid}/agent-candidates"))
    assert {c["name"] for c in cands} == {"架构 Agent", "开发 Agent"}
    assert (
        await dev.post(f"/api/v1/teams/{tid}/agents", {"atlas_agent_id": a1})
    ).status_code == 403
    ag1 = ok(await lead.post(f"/api/v1/teams/{tid}/agents", {"atlas_agent_id": a1}), 201)
    ag2 = ok(await lead.post(f"/api/v1/teams/{tid}/agents", {"atlas_agent_id": a2}), 201)

    # 项目：只能绑定已发布模板
    draft_only = ok(await admin.post("/api/v1/flow-templates", {"name": "未发布"}), 201)
    r = await lead.post(
        f"/api/v1/teams/{tid}/projects",
        {"name": "账号体系升级", "flow_template_id": draft_only["id"]},
    )
    assert r.json()["code"] == "TEMPLATE_NOT_PUBLISHED"
    assert (
        await dev.post(
            f"/api/v1/teams/{tid}/projects", {"name": "x", "flow_template_id": tpl["id"]}
        )
    ).status_code == 403
    p = ok(
        await lead.post(
            f"/api/v1/teams/{tid}/projects", {"name": "账号体系升级", "flow_template_id": tpl["id"]}
        ),
        201,
    )
    pid = p["id"]
    r = await lead.post(
        f"/api/v1/teams/{tid}/projects", {"name": "账号体系升级", "flow_template_id": tpl["id"]}
    )
    assert r.json()["code"] == "NAME_CONFLICT"

    roles = ok(await dev.get(f"/api/v1/projects/{pid}/roles"))
    assert roles["incomplete"] == 6
    arch = next(r for r in roles["roles"] if r["name"] == "架构")
    assert arch["problems"] == ["缺少人", "缺少 Agent"]

    put = lambda role, body: lead.put(f"/api/v1/projects/{pid}/roles/{role}", body)  # noqa: E731
    assert (await put("技术评审", {"agents": [ag1["id"]]})).json()["code"] == "AGENT_NOT_ALLOWED"
    assert (await put("架构", {"users": [w.id("outsider")]})).json()["code"] == "NOT_TEAM_MEMBER"
    assert (await put("架构", {"agents": [ag1["id"], ag2["id"]]})).json()[
        "code"
    ] == "TOO_MANY_AGENTS"
    assert (
        await dev.put(f"/api/v1/projects/{pid}/roles/架构", {"users": [w.id("dev")]})
    ).status_code == 403
    d = ok(await put("架构", {"users": [w.id("lead"), w.id("dev")], "agents": [ag1["id"]]}))
    arch = next(r for r in d["roles"]["roles"] if r["name"] == "架构")
    assert arch["problems"] == [] and [u["name"] for u in arch["users"]] == ["李工", "王工"]
    assert {
        n["node"] for n in next(r for r in d["roles"]["roles"] if r["name"] == "技术评审")["nodes"]
    } >= {"架构设计"}

    # 行版本冲突
    assert (await put("产品", {"users": [w.id("dev")], "version": d["version"] - 1})).json()[
        "code"
    ] == "STALE_ROW_VERSION"

    # Atlas 停用 Agent → 同步后不可用
    w.atlas.agents[a1]["status"] = "archived"
    ok(await lead.post(f"/api/v1/teams/{tid}/agents/sync"))
    d = ok(await lead.get(f"/api/v1/projects/{pid}"))
    assert "Agent 不可用" in next(r for r in d["roles"]["roles"] if r["name"] == "架构")["problems"]
    assert any(h["kind"] == "agent_unavailable" for h in d["hints"])

    # 移出 Agent：被使用时先确认
    r = await lead.delete(f"/api/v1/teams/{tid}/agents/{ag1['id']}")
    assert r.json()["code"] == "AGENT_IN_USE" and r.json()["details"]["usage"]
    ok(await lead.delete(f"/api/v1/teams/{tid}/agents/{ag1['id']}", params={"confirm": True}))

    # 移除成员：连带移除其角色分配
    m = ok(await lead.get(f"/api/v1/teams/{tid}/members"))
    dev_grant = next(g for g in m["grants"] if g["subject"]["id"] == w.id("dev"))
    r = ok(await lead.delete(f"/api/v1/teams/{tid}/members/{dev_grant['id']}"))
    assert r["removed_assignments"] == [
        {"project_name": "账号体系升级", "role": "架构", "user_id": w.id("dev")}
    ]

    # 归档：不能改分配；取消归档恢复
    ok(await lead.post(f"/api/v1/projects/{pid}/archive"))
    assert (await put("产品", {"users": [w.id("lead")]})).json()["code"] == "PROJECT_ARCHIVED"
    ok(await lead.post(f"/api/v1/projects/{pid}/unarchive"))
    ok(await put("产品", {"users": [w.id("lead")]}))

    # 绑定的模板被停用：项目提示；模板被绑定过不能删除
    ok(await admin.post(f"/api/v1/flow-templates/{tpl['id']}/disable"))
    assert any(
        h["kind"] == "template_disabled"
        for h in ok(await lead.get(f"/api/v1/projects/{pid}"))["hints"]
    )
    assert (await admin.delete(f"/api/v1/flow-templates/{tpl['id']}")).json()[
        "code"
    ] == "TEMPLATE_IN_USE"
