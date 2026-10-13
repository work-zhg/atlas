"""开放接口扩展（TeamFlow 需要）：校验密码、批量首写、授权明细、拥有者、operator 修改、改名等。"""

from __future__ import annotations

from uc_testkit import STRONG, Client, admin_client, make_dept, make_user, root_id


async def _app(c: Client) -> tuple[str, dict[str, str]]:
    r = await c.post("/api/v1/apps", {"name": "AI TeamFlow"})
    app_id, secret = r.json()["app"]["id"], r.json()["app_secret"]
    await c.put(f"/api/v1/apps/{app_id}/scope", {"all": True, "dept_ids": []})
    key = (await c.get(f"/api/v1/apps/{app_id}")).json()["app_key"]
    tok = (
        await c.http.post("/open/v1/auth/token", json={"app_key": key, "app_secret": secret})
    ).json()
    return app_id, {"Authorization": f"Bearer {tok['access_token']}"}


async def test_verify_password() -> None:
    c = await admin_client()
    root = await root_id(c)
    _, temp = await make_user(c, "zhang.ming", root)
    _, h = await _app(c)
    # 未改过临时密码：拒绝，先去用户中心改密
    r = await c.http.post(
        "/open/v1/auth/verify-password", json={"account": "zhang.ming", "password": temp}, headers=h
    )
    assert r.json()["code"] == "PASSWORD_CHANGE_REQUIRED"
    u = Client()
    await u.login_and_activate("zhang.ming", temp)
    r = await c.http.post(
        "/open/v1/auth/verify-password",
        json={"account": "zhang.ming", "password": STRONG},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["account"] == "zhang.ming" and r.json()["can_access"] is True
    r = await c.http.post(
        "/open/v1/auth/verify-password",
        json={"account": "zhang.ming", "password": "bad"},
        headers=h,
    )
    assert r.json()["code"] == "LOGIN_FAILED" and r.json()["details"]["left"] == 4  # 失败计数已落库
    await c.close()
    await u.close()


async def test_batch_acl_holders_operator_admin_rename() -> None:
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
            {"module": "团队", "code": "team:manage_all", "name": "管理全部团队"},
        )
    ).json()["id"]
    r = await c.post(
        f"/api/v1/apps/{app_id}/data-types",
        {"code": "Team", "name": "团队", "admin_operation_code": "team:nope"},
    )
    assert r.json()["code"] == "VALIDATION_FAILED"
    r = await c.post(
        f"/api/v1/apps/{app_id}/data-types",
        {"code": "Team", "name": "团队", "admin_operation_code": "team:manage_all"},
    )
    assert r.status_code == 201 and r.json()["admin_operation_code"] == "team:manage_all"

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

    # 数据管理员：王测拿到含 team:manage_all 的角色后，不是 Owner 也能改
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
