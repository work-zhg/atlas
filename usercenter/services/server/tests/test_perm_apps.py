"""应用接入 + 角色授权：继承、岗位默认角色、超级管理员保护、开放接口授权快照。"""

from __future__ import annotations

from uc_testkit import Client, admin_client, make_dept, make_user, role_id, root_id


async def _setup_app(c: Client) -> tuple[str, str, str]:
    """建 TeamFlow：两个操作、两个菜单（一个公共）、一个角色。返回 (app_id, role_id, secret)。"""
    r = await c.post("/api/v1/apps", {"name": "AI TeamFlow", "icon": "🧩"})
    assert r.status_code == 201, r.text
    app_id, secret = r.json()["app"]["id"], r.json()["app_secret"]
    ops = [
        (
            await c.post(
                f"/api/v1/apps/{app_id}/operations", {"module": "流程", "code": code, "name": code}
            )
        ).json()["id"]
        for code in ("process:view", "process:start")
    ]
    work = (
        await c.post(
            f"/api/v1/apps/{app_id}/menus", {"type": "dir", "code": "work", "name": "工作"}
        )
    ).json()["id"]
    await c.post(
        f"/api/v1/apps/{app_id}/menus",
        {
            "type": "menu",
            "code": "process",
            "name": "我的流程",
            "path": "/processes",
            "is_public": True,
            "parent_id": work,
        },
    )
    team = (
        await c.post(
            f"/api/v1/apps/{app_id}/menus",
            {"type": "menu", "code": "team", "name": "团队", "path": "/teams", "parent_id": work},
        )
    ).json()["id"]
    role = (
        await c.post(f"/api/v1/apps/{app_id}/roles", {"name": "流程成员", "code": "TF_MEMBER"})
    ).json()["id"]
    assert (await c.put(f"/api/v1/roles/{role}/operations", {"ids": ops})).status_code == 200
    assert (await c.put(f"/api/v1/roles/{role}/menus", {"ids": [team]})).status_code == 200
    return app_id, role, secret


async def _token(c: Client, app_id: str, secret: str) -> dict[str, str]:
    key = (await c.get(f"/api/v1/apps/{app_id}")).json()["app_key"]
    r = await c.http.post("/open/v1/auth/token", json={"app_key": key, "app_secret": secret})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_builtin_app_readonly_and_cross_app_rejected() -> None:
    c = await admin_client()
    apps = (await c.get("/api/v1/apps")).json()
    uc = next(a for a in apps if a["is_builtin"])
    r = await c.post(
        f"/api/v1/apps/{uc['id']}/operations", {"module": "x", "code": "x:y", "name": "x"}
    )
    assert r.json()["code"] == "BUILTIN_READONLY"
    r = await c.post(f"/api/v1/apps/{uc['id']}/disable")
    assert r.json()["code"] == "BUILTIN_READONLY"
    super_role = await role_id(c, "UC_SUPER")
    assert (await c.put(f"/api/v1/roles/{super_role}/operations", {"ids": []})).json()[
        "code"
    ] == "BUILTIN_READONLY"
    app_id, role, _ = await _setup_app(c)
    uc_op = (await c.get(f"/api/v1/apps/{uc['id']}/operations")).json()[0]["id"]
    r = await c.put(f"/api/v1/roles/{role}/operations", {"ids": [uc_op]})
    assert r.json()["code"] == "CROSS_APP_REFERENCE"
    r = await c.post(
        f"/api/v1/apps/{app_id}/operations", {"module": "x", "code": "BadCode", "name": "x"}
    )
    assert r.json()["code"] == "OP_CODE_INVALID"
    await c.close()


async def test_grant_inheritance_scope_and_snapshot() -> None:
    c = await admin_client()
    root = await root_id(c)
    rd = await make_dept(c, root, "研发中心")
    trade = await make_dept(c, rd, "交易中台")
    he, _ = await make_user(c, "he.chao", trade)
    app_id, role, secret = await _setup_app(c)
    h = await _token(c, app_id, secret)

    # 新应用默认无人可访问：快照为空
    snap = (await c.http.get(f"/open/v1/users/{he}/authz", headers=h)).json()
    assert snap["can_access"] is False and snap["permissions"] == [] and snap["menus"] == []

    await c.put(f"/api/v1/apps/{app_id}/scope", {"all": False, "dept_ids": [rd]})
    snap = (await c.http.get(f"/open/v1/users/{he}/authz", headers=h)).json()
    assert snap["can_access"] is True
    assert snap["permissions"] == []
    # 只有公共菜单「我的流程」，目录随之出现
    assert snap["menus"] == [
        {
            "name": "工作",
            "icon": None,
            "children": [
                {"code": "process", "name": "我的流程", "icon": None, "path": "/processes"}
            ],
        }
    ]

    # 授予研发中心（不含下级）→ 交易中台的何超不命中；改为含下级 → 命中
    r = await c.post(
        "/api/v1/grants",
        {
            "kind": "role",
            "target_ids": [role],
            "subjects": [{"type": "dept", "id": rd}],
            "include_sub": False,
        },
    )
    assert r.json() == {"added": 1, "updated": 0}
    assert (await c.http.get(f"/open/v1/users/{he}/authz", headers=h)).json()["permissions"] == []
    r = await c.post(
        "/api/v1/grants",
        {
            "kind": "role",
            "target_ids": [role],
            "subjects": [{"type": "dept", "id": rd}],
            "include_sub": True,
        },
    )
    assert r.json() == {"added": 0, "updated": 1}
    snap = (await c.http.get(f"/open/v1/users/{he}/authz", headers=h)).json()
    assert snap["roles"] == ["TF_MEMBER"] and snap["permissions"] == [
        "process:start",
        "process:view",
    ]
    assert [m["code"] for m in snap["menus"][0]["children"]] == ["process", "team"]
    r = await c.http.post(
        "/open/v1/authz/check", json={"user_id": he, "permission": "process:start"}, headers=h
    )
    assert r.json()["allowed"] is True

    # 继承授权在用户视角可见，来源是部门
    sg = (await c.get(f"/api/v1/subjects/user/{he}/grants")).json()
    assert sg["own"] == [] and sg["inherited"][0]["subject"]["name"] == "研发中心"
    holders = (await c.get(f"/api/v1/roles/{role}/holders")).json()
    assert holders[0]["sources"] == ["部门：研发中心（含下级）"]

    # 停用应用：接口令牌立即失效
    await c.post(f"/api/v1/apps/{app_id}/disable")
    assert (await c.http.get(f"/open/v1/users/{he}/authz", headers=h)).status_code == 401
    await c.close()


async def test_positions_default_roles_and_super_guard() -> None:
    c = await admin_client()
    root = await root_id(c)
    li, temp = await make_user(c, "li.gong", root)
    admin_role = await role_id(c, "UC_USER_ADMIN")
    super_role = await role_id(c, "UC_SUPER")
    r = await c.post(
        "/api/v1/positions", {"code": "HR_LEAD", "name": "HR 负责人", "role_ids": [admin_role]}
    )
    pos = r.json()["id"]
    await c.post(
        "/api/v1/grants",
        {"kind": "position", "target_ids": [pos], "subjects": [{"type": "user", "id": li}]},
    )
    u = Client()
    await u.login_and_activate("li.gong", temp)
    me = (await u.get("/api/v1/auth/me")).json()
    assert "user:create" in me["permissions"] and "app:manage" not in me["permissions"]
    assert (await c.get(f"/api/v1/users/{li}")).json()["positions"] == ["HR 负责人"]
    r = await c.delete(f"/api/v1/positions/{pos}")
    assert r.json()["code"] == "POSITION_IN_USE"

    # 唯一的超级管理员授权不能撤销
    grants = (await c.get("/api/v1/grants", params={"kind": "role"})).json()["items"]
    super_grant = next(g for g in grants if g["target"]["id"] == super_role)
    r = await c.delete(f"/api/v1/grants/{super_grant['id']}")
    assert r.json()["code"] == "LAST_SUPER_ADMIN"
    # 先让李工也成为超级管理员，再撤销 admin 的就可以
    await c.post(
        "/api/v1/grants",
        {"kind": "role", "target_ids": [super_role], "subjects": [{"type": "user", "id": li}]},
    )
    assert (await c.delete(f"/api/v1/grants/{super_grant['id']}")).status_code == 200
    # 现在李工是唯一超级管理员：停用他被拒
    assert (await u.post(f"/api/v1/users/{li}/disable", {})).json()["code"] == "CANNOT_DISABLE_SELF"
    await c.close()
    await u.close()


async def test_open_api_users_scope_and_disabled() -> None:
    c = await admin_client()
    root = await root_id(c)
    rd = await make_dept(c, root, "研发中心")
    other = await make_dept(c, root, "产品部")
    a, _ = await make_user(c, "a.dev", rd)
    await make_user(c, "b.pm", other)
    app_id, _, secret = await _setup_app(c)
    h = await _token(c, app_id, secret)
    await c.put(f"/api/v1/apps/{app_id}/scope", {"all": False, "dept_ids": [rd]})
    users = (await c.http.get("/open/v1/users", headers=h)).json()
    assert [u["account"] for u in users["items"]] == ["a.dev"]
    info = (await c.http.get("/open/v1/users/by-account/a.dev", headers=h)).json()
    assert info["dept"]["path"] == "研发中心"
    await c.post(f"/api/v1/users/{a}/disable", {})
    snap = (await c.http.get(f"/open/v1/users/{a}/authz", headers=h)).json()
    assert snap["user"]["status"] == "disabled" and snap["can_access"] is False
    await c.close()
