"""开放接口扩展（TeamFlow 需要）：请求头鉴权、批量首写、授权明细、拥有者、operator 修改、改名等。"""

from __future__ import annotations

from uc_testkit import Client, admin_client, make_dept, make_user, root_id


async def _app(c: Client) -> tuple[str, dict[str, str]]:
    r = await c.post("/api/v1/apps", {"name": "AI TeamFlow", "code": "teamflow"})
    app_id = r.json()["app"]["id"]
    await c.put(f"/api/v1/apps/{app_id}/scope", {"all": True, "dept_ids": []})
    return app_id, {"X-Caller-App": "teamflow"}


async def test_open_api_identity_headers() -> None:
    """内网免令牌（内网身份传递 §3、§11）：调用方应用 = X-Caller-App；租户 = X-Tenant-Id，
    缺省按 X-Uid 推，再缺省取唯一租户。审计记录谁在用（X-Uid）。"""
    c = await admin_client()
    root = await root_id(c)
    zhang, _ = await make_user(c, "zhang.ming", root)
    app_id, h = await _app(c)
    # 没有 / 不认识的调用方应用
    assert (await c.http.get("/open/v1/depts/tree")).json()["code"] == "MISSING_CALLER_APP"
    r = await c.http.get("/open/v1/depts/tree", headers={"X-Caller-App": "nope"})
    assert r.status_code == 403 and r.json()["code"] == "APP_NOT_ALLOWED"
    # 令牌不再被开放接口认可：只看请求头
    r = await c.http.get("/open/v1/depts/tree", headers={"Authorization": "Bearer whatever"})
    assert r.status_code == 401
    # 单租户：不带租户也行；带了按带的
    assert (await c.http.get("/open/v1/depts/tree", headers=h)).status_code == 200
    tenant = (await c.get("/api/v1/auth/me")).json()["tenant"]["id"]
    r = await c.http.get(f"/open/v1/users/{zhang}", headers={**h, "X-Tenant-Id": tenant})
    assert r.status_code == 200 and r.json()["account"] == "zhang.ming"
    r = await c.http.get("/open/v1/depts/tree", headers={**h, "X-Tenant-Id": "not-a-uuid"})
    assert r.status_code == 400 and r.json()["code"] == "TENANT_NOT_FOUND"
    # 应用停用 → 403
    assert (await c.post(f"/api/v1/apps/{app_id}/disable")).status_code == 200
    assert (await c.http.get("/open/v1/depts/tree", headers=h)).status_code == 403
    assert (await c.post(f"/api/v1/apps/{app_id}/enable")).status_code == 200
    # 编码校验与唯一
    r = await c.post("/api/v1/apps", {"name": "另一个", "code": "teamflow"})
    assert r.json()["code"] == "APP_CODE_EXISTS"
    r = await c.post("/api/v1/apps", {"name": "另一个", "code": "Bad Code"})
    assert r.json()["code"] == "APP_CODE_INVALID"
    await c.close()


async def test_batch_acl_holders_operator_type_owner_rename() -> None:
    c = await admin_client()
    root = await root_id(c)
    rd = await make_dept(c, root, "研发中心")
    li, _ = await make_user(c, "li.gong", rd)
    he, _ = await make_user(c, "he.chao", rd)
    wang, _ = await make_user(c, "wang.ce", root)
    app_id, h = await _app(c)
    op = (
        await c.post(
            f"/api/v1/apps/{app_id}/operations",
            {"module": "团队", "code": "team:member", "name": "管理团队成员"},
        )
    ).json()["id"]
    r = await c.post(f"/api/v1/apps/{app_id}/data-types", {"code": "Team", "name": "团队"})
    assert r.status_code == 201 and "admin_operation_code" not in r.json()
    team_type = r.json()["id"]

    base = {"data_code": "Team", "data_id": "t1", "data_name": "交易中台"}
    # 首写必须含 OWNER
    r = await c.http.post(
        "/open/v1/data-permissions/batch",
        json={
            **base,
            "grants": [{"permission": "WRITE", "subject_type": "USER", "subject_id": he}],
        },
        headers=h,
    )
    assert r.json()["code"] == "OWNER_REQUIRED"
    r = await c.http.post(
        "/open/v1/data-permissions/batch",
        json={
            **base,
            "grants": [
                {"permission": "OWNER", "subject_type": "USER", "subject_account": "li.gong"},
                {
                    "permission": "WRITE",
                    "subject_type": "DEPT",
                    "subject_id": rd,
                    "include_sub": True,
                },
            ],
        },
        headers=h,
    )
    assert r.status_code == 201 and r.json()["added"] == 2, r.text
    # 已有授权后不带 operator 不行
    r = await c.http.post(
        "/open/v1/data-permissions/batch",
        json={
            **base,
            "grants": [{"permission": "WRITE", "subject_type": "USER", "subject_id": wang}],
        },
        headers=h,
    )
    assert r.json()["code"] == "OWNER_REQUIRED"

    acl = (
        await c.http.get(
            "/open/v1/data-permissions/acl",
            params={"data_code": "Team", "data_id": "t1"},
            headers=h,
        )
    ).json()
    assert [a["permission"] for a in acl] == ["OWNER", "WRITE"]
    holders = (
        await c.http.get(
            "/open/v1/data-permissions/holders",
            params={"data_code": "Team", "data_id": "t1", "min": "WRITE"},
            headers=h,
        )
    ).json()
    assert {x["user"]["account"]: x["permission"] for x in holders} == {
        "li.gong": "OWNER",
        "he.chao": "WRITE",
    }

    # operator 不是 Owner：拒绝；是 Owner：可以把何超设为 Owner
    r = await c.http.post(
        "/open/v1/data-permissions/batch",
        json={
            **base,
            "operator_id": he,
            "grants": [{"permission": "OWNER", "subject_type": "USER", "subject_id": he}],
        },
        headers=h,
    )
    assert r.json()["code"] == "OWNER_REQUIRED"
    r = await c.http.post(
        "/open/v1/data-permissions/batch",
        json={
            **base,
            "operator_id": li,
            "grants": [{"permission": "OWNER", "subject_type": "USER", "subject_id": he}],
        },
        headers=h,
    )
    assert r.status_code == 201
    he_acl = next(
        a
        for a in (
            await c.http.get(
                "/open/v1/data-permissions/acl",
                params={"data_code": "Team", "data_id": "t1"},
                headers=h,
            )
        ).json()
        if a["subject"].get("account") == "he.chao"
    )
    r = await c.http.patch(
        f"/open/v1/data-permissions/acl/{he_acl['id']}",
        json={"operator_id": li, "permission": "WRITE"},
        headers=h,
    )
    assert r.status_code == 200
    r = await c.http.delete(
        f"/open/v1/data-permissions/acl/{he_acl['id']}", params={"operator_id": wang}, headers=h
    )
    assert r.json()["code"] == "NOT_DATA_OWNER"

    # ★ 角色不影响数据权限：王测拿到平台管理员角色（含操作码）后，仍然改不了
    role = (
        await c.post(
            f"/api/v1/apps/{app_id}/roles", {"name": "平台管理员", "code": "TF_PLATFORM_ADMIN"}
        )
    ).json()["id"]
    await c.put(f"/api/v1/roles/{role}/operations", {"ids": [op]})
    await c.post(
        "/api/v1/grants",
        {"kind": "role", "target_ids": [role], "subjects": [{"type": "user", "id": wang}]},
    )
    r = await c.http.delete(
        f"/open/v1/data-permissions/acl/{he_acl['id']}", params={"operator_id": wang}, headers=h
    )
    assert r.json()["code"] == "NOT_DATA_OWNER"

    # 数据编码级授权：王测成为「全部团队 · Owner」后，不是这条数据的 Owner 也能改
    r = await c.post(
        f"/api/v1/data-types/{team_type}/acl",
        {"level": "OWNER", "subjects": [{"type": "user", "id": wang}]},
    )
    assert r.status_code == 201, r.text
    lv = (
        await c.http.get(
            "/open/v1/data-permissions/type",
            params={"data_code": "Team", "user_id": wang},
            headers=h,
        )
    ).json()
    assert lv["permission"] == "OWNER" and lv["sources"][0]["scope"] == "data_type"
    chk = (
        await c.http.get(
            "/open/v1/data-permissions/check",
            params={"data_code": "Team", "data_id": "t1", "user_id": wang},
            headers=h,
        )
    ).json()
    assert chk["permission"] == "OWNER"
    acc = (
        await c.http.get(
            "/open/v1/data-permissions/accessible",
            params={"data_code": "Team", "user_id": wang, "min": "WRITE"},
            headers=h,
        )
    ).json()
    assert acc["type_permission"] == "OWNER" and [i["data_id"] for i in acc["items"]] == ["t1"]
    holders = (
        await c.http.get(
            "/open/v1/data-permissions/holders",
            params={"data_code": "Team", "data_id": "t1", "min": "READ"},
            headers=h,
        )
    ).json()
    assert "wang.ce" not in {x["user"]["account"] for x in holders}, "编码级授权不是这条数据的成员"
    r = await c.http.delete(
        f"/open/v1/data-permissions/acl/{he_acl['id']}", params={"operator_id": wang}, headers=h
    )
    assert r.status_code == 200, r.text

    # 改名
    r = await c.http.patch(
        "/open/v1/data-objects",
        json={"data_code": "Team", "data_id": "t1", "data_name": "交易中台（新）"},
        headers=h,
    )
    assert r.status_code == 200
    data = (await c.get("/api/v1/data", params={"scope": "all"})).json()["items"]
    assert data[0]["data_name"] == "交易中台（新）"
    await c.close()


async def test_type_acl_is_managed_in_the_data_domain() -> None:
    """数据编码级授权：权限管理员（data:type_grant）或该编码的 Owner 可以改；部门含下级照常生效。"""
    c = await admin_client()
    root = await root_id(c)
    rd = await make_dept(c, root, "研发中心")
    sub = await make_dept(c, rd, "交易组")
    li, _ = await make_user(c, "li.gong", sub)
    wang, wang_temp = await make_user(c, "wang.ce", root)
    app_id, h = await _app(c)
    r = await c.post(f"/api/v1/apps/{app_id}/data-types", {"code": "Team", "name": "团队"})
    dt = r.json()

    async def level(uid: str) -> str:
        r = await c.http.get(
            "/open/v1/data-permissions/type",
            params={"data_code": "Team", "user_id": uid},
            headers=h,
        )
        return r.json()["permission"]

    # 部门含下级 · 只读：下级部门的人也拿到
    r = await c.post(
        f"/api/v1/data-types/{dt['id']}/acl",
        {"level": "READ", "subjects": [{"type": "dept", "id": rd}], "include_sub": True},
    )
    assert r.status_code == 201
    assert await level(li) == "READ" and await level(wang) == "NONE"

    # 普通用户既不是编码 Owner 也没有 data:type_grant：不能改
    u = Client()
    await u.login_and_activate("wang.ce", wang_temp)
    r = await u.post(
        f"/api/v1/data-types/{dt['id']}/acl",
        {"level": "OWNER", "subjects": [{"type": "user", "id": wang}]},
    )
    assert r.json()["code"] == "NOT_DATA_TYPE_OWNER"

    # 成为编码 Owner 后可以自己维护这个编码的授权
    await c.post(
        f"/api/v1/data-types/{dt['id']}/acl",
        {"level": "OWNER", "subjects": [{"type": "user", "id": wang}]},
    )
    view = (await u.get(f"/api/v1/data-types/{dt['id']}/acl")).json()
    assert view["my_level"] == "OWNER" and view["can_manage"] is True
    dept_row = next(a for a in view["acl"] if a["subject"]["type"] == "DEPT")
    r = await u.patch(f"/api/v1/data-type-acl/{dept_row['id']}", {"level": "WRITE"})
    assert r.status_code == 200, r.text
    assert await level(li) == "WRITE"
    r = await u.delete(f"/api/v1/data-type-acl/{dept_row['id']}")
    assert r.status_code == 200
    assert await level(li) == "NONE"
    await c.close()
    await u.close()
