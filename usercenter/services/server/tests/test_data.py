"""数据授权：接口写入首个 Owner、Owner 规则、级别取最高、Owner 保护、应用隔离。"""

from __future__ import annotations

from uc_testkit import Client, admin_client, make_dept, make_user, root_id


async def _atlas(c: Client) -> tuple[str, dict[str, str]]:
    r = await c.post("/api/v1/apps", {"name": "Atlas"})
    app_id, secret = r.json()["app"]["id"], r.json()["app_secret"]
    assert (
        await c.post(f"/api/v1/apps/{app_id}/data-types", {"code": "Agent", "name": "智能体"})
    ).status_code == 201
    key = (await c.get(f"/api/v1/apps/{app_id}")).json()["app_key"]
    tok = (
        await c.http.post("/open/v1/auth/token", json={"app_key": key, "app_secret": secret})
    ).json()["access_token"]
    return app_id, {"Authorization": f"Bearer {tok}"}


async def test_data_permission_flow() -> None:
    admin = await admin_client()
    root = await root_id(admin)
    rd = await make_dept(admin, root, "研发中心")
    infra = await make_dept(admin, rd, "技术中心")
    zhang, zt = await make_user(admin, "zhang.ming", root)
    chen, ct = await make_user(admin, "chen.yun", infra)
    _app_id, h = await _atlas(admin)

    write = {
        "data_code": "Agent",
        "data_id": "agt_1",
        "data_name": "代码评审助手",
        "subject_type": "USER",
    }
    # 第一条必须是 OWNER
    r = await admin.http.post(
        "/open/v1/data-permissions",
        json={**write, "permission": "READ", "subject_id": zhang},
        headers=h,
    )
    assert r.json()["code"] == "OWNER_REQUIRED"
    r = await admin.http.post(
        "/open/v1/data-permissions",
        json={**write, "permission": "OWNER", "subject_account": "zhang.ming"},
        headers=h,
    )
    assert r.status_code == 201 and r.json()["data_object_created"] is True
    # 已有授权后，应用不带 operator 不能再改
    r = await admin.http.post(
        "/open/v1/data-permissions",
        json={**write, "permission": "OWNER", "subject_id": chen},
        headers=h,
    )
    assert r.json()["code"] == "OWNER_REQUIRED"
    # 带上 Owner 作为 operator：给研发中心（含下级）只读
    r = await admin.http.post(
        "/open/v1/data-permissions",
        json={
            **write,
            "permission": "READ",
            "subject_type": "DEPT",
            "subject_id": rd,
            "include_sub": True,
            "operator_id": zhang,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text

    # 管理台：张明（Owner）再给陈运读写；陈运有效级别取最高
    z = Client()
    await z.login_and_activate("zhang.ming", zt)
    mine = (await z.get("/api/v1/data", params={"scope": "mine"})).json()
    obj = mine["items"][0]["id"]
    assert mine["counts"]["mine"] == 1
    r = await z.post(
        f"/api/v1/data/{obj}/acl", {"level": "WRITE", "subjects": [{"type": "user", "id": chen}]}
    )
    assert r.status_code == 201, r.text
    chk = (
        await admin.http.get(
            "/open/v1/data-permissions/check",
            params={"data_code": "Agent", "data_id": "agt_1", "user_id": chen},
            headers=h,
        )
    ).json()
    assert chk["permission"] == "WRITE" and len(chk["sources"]) == 2
    acc = (
        await admin.http.get(
            "/open/v1/data-permissions/accessible",
            params={"data_code": "Agent", "user_id": chen, "min": "WRITE"},
            headers=h,
        )
    ).json()
    assert acc["items"] == [{"data_id": "agt_1", "permission": "WRITE"}]

    # 非 Owner 不能改授权
    ch = Client()
    await ch.login_and_activate("chen.yun", ct)
    r = await ch.post(
        f"/api/v1/data/{obj}/acl", {"level": "OWNER", "subjects": [{"type": "user", "id": chen}]}
    )
    assert r.json()["code"] == "NOT_DATA_OWNER"
    # 管理员（data:view）能看全部但不能改
    assert (await admin.get(f"/api/v1/data/{obj}")).json()["is_owner"] is False
    assert (
        await admin.post(
            f"/api/v1/data/{obj}/acl", {"level": "READ", "subjects": [{"type": "user", "id": chen}]}
        )
    ).json()["code"] == "NOT_DATA_OWNER"

    # Owner 保护：移除唯一 Owner（自己）被拒
    detail = (await z.get(f"/api/v1/data/{obj}")).json()
    own_acl = next(a for a in detail["acl"] if a["level"] == "OWNER")
    assert (await z.delete(f"/api/v1/data-acl/{own_acl['id']}")).json()["code"] == "LAST_DATA_OWNER"
    assert (await z.patch(f"/api/v1/data-acl/{own_acl['id']}", {"level": "READ"})).json()[
        "code"
    ] == "LAST_DATA_OWNER"
    # 先把陈运升为 Owner，再降自己就可以
    chen_acl = next(a for a in detail["acl"] if a["subject"]["id"] == chen)
    assert (
        await z.patch(f"/api/v1/data-acl/{chen_acl['id']}", {"level": "OWNER"})
    ).status_code == 200
    assert (
        await z.patch(f"/api/v1/data-acl/{own_acl['id']}", {"level": "READ"})
    ).status_code == 200

    # 清理
    r = await admin.http.delete(
        "/open/v1/data-permissions", params={"data_code": "Agent", "data_id": "agt_1"}, headers=h
    )
    assert r.json()["deleted_acls"] == 3
    for cl in (admin, z, ch):
        await cl.close()


async def test_data_code_isolated_per_app() -> None:
    admin = await admin_client()
    root = await root_id(admin)
    u, _ = await make_user(admin, "x.user", root)
    await _atlas(admin)
    r = await admin.post("/api/v1/apps", {"name": "TeamFlow"})
    other_id, secret = r.json()["app"]["id"], r.json()["app_secret"]
    key = (await admin.get(f"/api/v1/apps/{other_id}")).json()["app_key"]
    tok = (
        await admin.http.post("/open/v1/auth/token", json={"app_key": key, "app_secret": secret})
    ).json()["access_token"]
    r = await admin.http.post(
        "/open/v1/data-permissions",
        json={
            "data_code": "Agent",
            "data_id": "a1",
            "permission": "OWNER",
            "subject_type": "USER",
            "subject_id": u,
        },
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.json()["code"] == "DATA_TYPE_NOT_OWNED"
    await admin.close()
