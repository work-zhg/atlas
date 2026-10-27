"""数字员工（doc/digital-employee-design.html §3–§4）。

一种用户：接入应用经开放接口创建、维护；和人一样被授权；不能登录、不进组织树（没有部门，
部门授权落不到它身上）、不能持有用户中心的角色和「全部数据」授权、不占人员配额。
"""

from __future__ import annotations

from typing import Any

from uc_testkit import Client, admin_client, make_dept, make_user, role_id, root_id

H = {"X-Caller-App": "atlas"}


async def _app(c: Client, name: str = "Atlas", code: str = "atlas") -> str:
    r = await c.post("/api/v1/apps", {"name": name, "code": code})
    assert r.status_code == 201, r.text
    app_id = r.json()["app"]["id"]
    await c.put(f"/api/v1/apps/{app_id}/scope", {"all": True, "dept_ids": []})
    return app_id


async def _create(c: Client, ref: str, owner: str, name: str = "🤖 HR 助手", **h: str) -> Any:
    return await c.http.post(
        "/open/v1/digital-employees",
        json={"external_ref": ref, "name": name, "owner_id": owner},
        headers={**H, **h},
    )


async def test_create_is_idempotent_and_outside_org_tree() -> None:
    c = await admin_client()
    root = await root_id(c)
    rd = await make_dept(c, root, "研发中心")
    zhang, _ = await make_user(c, "zhang.ming", rd)
    await _app(c)

    r = await _create(c, "agent-1", zhang)
    assert r.status_code == 201, r.text
    de = r.json()
    assert de["user_type"] == "digital" and de["account"].startswith("dw-")
    assert len(de["account"]) == 13 and de["owner_id"] == zhang
    assert de["source_app"] == "atlas" and de["external_ref"] == "agent-1"
    # 幂等：同一外部引用返回同一个
    again = await _create(c, "agent-1", zhang, name="改了名也一样")
    assert again.status_code == 200 and again.json()["id"] == de["id"]
    found = await c.http.get(
        "/open/v1/digital-employees", params={"external_ref": "agent-1"}, headers=H
    )
    assert found.json()["id"] == de["id"]
    # 名字超长截断而不是失败
    long = await _create(c, "agent-2", zhang, name="🤖 " + "很长的名字" * 10)
    assert long.status_code == 201 and len(long.json()["name"]) == 32

    # 用户信息：没有部门、没有直属上级
    info = (await c.http.get(f"/open/v1/users/{de['id']}", headers=H)).json()
    assert info["user_type"] == "digital" and info["dept"] is None and info["manager"] is None
    assert info["email"] is None and info["owner_id"] == zhang

    # 不进组织树：部门成员、按部门选人、管理台默认列表都没有它
    members = (await c.get(f"/api/v1/depts/{root}/members", params={"include_sub": True})).json()
    assert de["id"] not in [u["id"] for u in members["items"]]
    picked = (await c.http.get("/open/v1/users", headers=H)).json()
    assert de["id"] not in [u["id"] for u in picked["items"]]
    both = (await c.http.get("/open/v1/users", params={"types": "human,digital"}, headers=H)).json()
    assert de["id"] in [u["id"] for u in both["items"]]
    by_dept = (
        await c.http.get(
            "/open/v1/users", params={"types": "human,digital", "dept_id": root}, headers=H
        )
    ).json()
    assert de["id"] not in [u["id"] for u in by_dept["items"]]
    listed = (await c.get("/api/v1/users")).json()
    assert de["id"] not in [u["id"] for u in listed["items"]]
    digital = (await c.get("/api/v1/users", params={"type": "digital"})).json()
    row = next(u for u in digital["items"] if u["id"] == de["id"])
    assert row["status"] == "active" and row["owner"]["id"] == zhang
    assert row["source_app"]["code"] == "atlas" and row["dept_id"] is None
    # 通讯录：缺省只有人；授权选人时连数字员工一起
    d0 = (await c.get("/api/v1/directory/users", params={"q": "dw-"})).json()
    assert d0 == []
    d1 = (await c.get("/api/v1/directory/users", params={"q": "dw-", "digital": True})).json()
    assert {u["id"] for u in d1} >= {de["id"]} and d1[0]["user_type"] == "digital"
    await c.close()


async def test_owner_rules_and_only_source_app_writes() -> None:
    c = await admin_client()
    root = await root_id(c)
    zhang, _ = await make_user(c, "zhang.ming", root)
    li, _ = await make_user(c, "li.hua", root)
    await _app(c)
    await _app(c, "AI TeamFlow", "teamflow")

    de = (await _create(c, "agent-1", zhang)).json()
    # 负责人必须是人
    r = await _create(c, "agent-x", de["id"])
    assert r.json()["code"] == "OWNER_NOT_HUMAN"
    # 别的应用：能读，不能改
    tf = {"X-Caller-App": "teamflow"}
    assert (
        await c.http.get(f"/open/v1/digital-employees/{de['id']}", headers=tf)
    ).status_code == 200
    r = await c.http.patch(f"/open/v1/digital-employees/{de['id']}", json={"name": "x"}, headers=tf)
    assert r.status_code == 403 and r.json()["code"] == "NOT_SOURCE_APP"
    # 来源应用：改名、换负责人、停用、启用
    r = await c.http.patch(
        f"/open/v1/digital-employees/{de['id']}",
        json={"name": "🤖 新名字", "owner_id": li},
        headers=H,
    )
    assert r.status_code == 200 and r.json()["name"] == "🤖 新名字" and r.json()["owner_id"] == li
    r = await c.http.post(f"/open/v1/digital-employees/{de['id']}/disable", headers=H)
    assert r.json()["status"] == "disabled"
    snap = (await c.http.get(f"/open/v1/users/{de['id']}/authz", headers=H)).json()
    assert snap["can_access"] is False
    r = await c.http.post(f"/open/v1/digital-employees/{de['id']}/enable", headers=H)
    assert r.json()["status"] == "active"
    # 人的 id 不是数字员工
    r = await c.http.get(f"/open/v1/digital-employees/{zhang}", headers=H)
    assert r.status_code == 404
    await c.close()


async def test_cannot_login_and_console_is_readonly() -> None:
    c = await admin_client()
    root = await root_id(c)
    zhang, _ = await make_user(c, "zhang.ming", root)
    await _app(c)
    de = (await _create(c, "agent-1", zhang)).json()

    anon = Client()
    r = await anon.login(de["account"], "whatever", tenant="default")
    assert r.status_code == 401
    await anon.close()
    for path in ("reset-password", "unlock"):
        r = await c.post(f"/api/v1/users/{de['id']}/{path}")
        assert r.json()["code"] == "DIGITAL_READONLY", path
    r = await c.patch(f"/api/v1/users/{de['id']}", {"name": "改"})
    assert r.json()["code"] == "DIGITAL_READONLY"
    # 管理台可以停用 / 启用（紧急止血）
    assert (await c.post(f"/api/v1/users/{de['id']}/disable", {})).json()["status"] == "disabled"
    assert (await c.post(f"/api/v1/users/{de['id']}/enable")).json()["status"] == "active"
    # 不能做部门负责人、不能调入部门
    r = await c.patch(f"/api/v1/depts/{root}", {"leader_id": de["id"]})
    assert r.json()["code"] == "DIGITAL_NOT_ALLOWED"
    r = await c.post(f"/api/v1/depts/{root}/members/move-in", {"user_ids": [de["id"]]})
    assert r.json()["code"] == "DIGITAL_NOT_ALLOWED"
    await c.close()


async def test_only_direct_grants_count() -> None:
    """部门授权（含下级）落不到数字员工身上；直接授给它的才算。"""
    c = await admin_client()
    root = await root_id(c)
    zhang, _ = await make_user(c, "zhang.ming", root)
    app_id = await _app(c)
    op = (
        await c.post(
            f"/api/v1/apps/{app_id}/operations",
            {"module": "知识库", "code": "kb:create", "name": "建知识库"},
        )
    ).json()["id"]
    role = (
        await c.post(f"/api/v1/apps/{app_id}/roles", {"name": "知识库建设者", "code": "KB_BUILDER"})
    ).json()["id"]
    await c.put(f"/api/v1/roles/{role}/operations", {"ids": [op]})
    de = (await _create(c, "agent-1", zhang)).json()

    async def perms(uid: str) -> list[str]:
        return (await c.http.get(f"/open/v1/users/{uid}/authz", headers=H)).json()["permissions"]

    r = await c.post(
        "/api/v1/grants",
        {
            "kind": "role",
            "target_ids": [role],
            "subjects": [{"type": "dept", "id": root}],
            "include_sub": True,
        },
    )
    assert r.status_code == 201, r.text
    assert "kb:create" in await perms(zhang)
    assert await perms(de["id"]) == []
    r = await c.post(
        "/api/v1/grants",
        {"kind": "role", "target_ids": [role], "subjects": [{"type": "user", "id": de["id"]}]},
    )
    assert r.status_code == 201, r.text
    assert await perms(de["id"]) == ["kb:create"]
    own = (await c.get(f"/api/v1/subjects/user/{de['id']}/grants")).json()
    assert len(own["own"]) == 1 and own["inherited"] == []

    # 用户中心自身的角色不能授给数字员工（直接或经岗位）
    super_role = await role_id(c, "UC_SUPER")
    r = await c.post(
        "/api/v1/grants",
        {
            "kind": "role",
            "target_ids": [super_role],
            "subjects": [{"type": "user", "id": de["id"]}],
        },
    )
    assert r.json()["code"] == "DIGITAL_NOT_ALLOWED"
    pos = (
        await c.post(
            "/api/v1/positions",
            {"code": "UC_ADMIN_POS", "name": "管理岗", "role_ids": [super_role]},
        )
    ).json()["id"]
    r = await c.post(
        "/api/v1/grants",
        {"kind": "position", "target_ids": [pos], "subjects": [{"type": "user", "id": de["id"]}]},
    )
    assert r.json()["code"] == "DIGITAL_NOT_ALLOWED"
    await c.close()


async def test_data_permissions() -> None:
    """单条数据可以共享给数字员工；部门 ACL 落不到它身上；「全部数据」授权不能授给它。"""
    c = await admin_client()
    root = await root_id(c)
    zhang, _ = await make_user(c, "zhang.ming", root)
    app_id = await _app(c)
    dt = (
        await c.post(
            f"/api/v1/apps/{app_id}/data-types", {"code": "AtlasKnowledgeBase", "name": "知识库"}
        )
    ).json()
    de = (await _create(c, "agent-1", zhang)).json()

    async def check(uid: str) -> str:
        r = await c.http.get(
            "/open/v1/data-permissions/check",
            params={"data_code": "AtlasKnowledgeBase", "data_id": "kb-1", "user_id": uid},
            headers=H,
        )
        return r.json()["permission"]

    r = await c.http.post(
        "/open/v1/data-permissions/batch",
        json={
            "data_code": "AtlasKnowledgeBase",
            "data_id": "kb-1",
            "data_name": "HR 制度库",
            "grants": [
                {"permission": "OWNER", "subject_type": "USER", "subject_id": zhang},
                {
                    "permission": "READ",
                    "subject_type": "DEPT",
                    "subject_id": root,
                    "include_sub": True,
                },
            ],
        },
        headers=H,
    )
    assert r.status_code == 201, r.text
    assert await check(zhang) == "OWNER" and await check(de["id"]) == "NONE"
    r = await c.http.post(
        "/open/v1/data-permissions",
        json={
            "data_code": "AtlasKnowledgeBase",
            "data_id": "kb-1",
            "permission": "READ",
            "subject_type": "USER",
            "subject_id": de["id"],
            "operator_id": zhang,
        },
        headers=H,
    )
    assert r.status_code == 201, r.text
    assert await check(de["id"]) == "READ"
    acc = (
        await c.http.get(
            "/open/v1/data-permissions/accessible",
            params={"data_code": "AtlasKnowledgeBase", "user_id": de["id"]},
            headers=H,
        )
    ).json()
    assert [i["data_id"] for i in acc["items"]] == ["kb-1"]
    # 持有人列表里数字员工也能显示（没有部门）
    obj = (await c.get("/api/v1/data", params={"scope": "all", "q": "HR"})).json()["items"][0]["id"]
    detail = (await c.get(f"/api/v1/data/{obj}")).json()
    assert any(h["user"]["id"] == de["id"] for h in detail["holders"])

    r = await c.post(
        f"/api/v1/data-types/{dt['id']}/acl",
        {"level": "OWNER", "subjects": [{"type": "user", "id": de["id"]}]},
    )
    assert r.json()["code"] == "DIGITAL_NOT_ALLOWED"
    await c.close()


async def test_not_counted_in_quota() -> None:
    """人员配额只算人：数字员工不占，也不因配额满而建不出来。"""
    c = await admin_client()
    root = await root_id(c)
    zhang, _ = await make_user(c, "zhang.ming", root)
    await _app(c)
    me = (await c.get("/api/v1/auth/me")).json()
    tenant = me["tenant"]["id"]
    r = await c.patch(f"/api/v1/tenants/{tenant}", {"max_users": 2})  # admin + zhang
    assert r.status_code == 200, r.text
    assert (await _create(c, "agent-1", zhang)).status_code == 201
    r = await c.post(
        "/api/v1/users",
        {"name": "新人", "account": "xin.ren", "email": "x@xinghai.com", "dept_id": root},
    )
    assert r.json()["code"] == "TENANT_QUOTA_EXCEEDED"
    row = next(t for t in (await c.get("/api/v1/tenants")).json() if t["id"] == tenant)
    assert row["users"] == 2
    await c.close()
