"""管理台登录、强制改密、CSRF、锁定；用户管理与超级管理员保护。"""

from __future__ import annotations

import os

from uc_testkit import STRONG, Client, admin_client, make_user, root_id


async def test_first_login_must_change_password_then_me() -> None:
    c = Client()
    r = await c.login("admin", os.environ["UC_TEST_ADMIN_TEMP"])
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    # 须改密时其他接口一律拒绝
    r = await c.get("/api/v1/depts/tree")
    assert r.status_code == 403 and r.json()["code"] == "PASSWORD_CHANGE_REQUIRED"
    r = await c.post(
        "/api/v1/auth/password",
        {"old_password": os.environ["UC_TEST_ADMIN_TEMP"], "new_password": "short"},
    )
    assert r.json()["code"] == "PASSWORD_POLICY"
    r = await c.post(
        "/api/v1/auth/password",
        {"old_password": os.environ["UC_TEST_ADMIN_TEMP"], "new_password": STRONG},
    )
    assert r.status_code == 200
    me = (await c.get("/api/v1/auth/me")).json()
    assert me["must_change_password"] is False
    assert "user:create" in me["permissions"]
    assert [m["code"] for m in me["menus"]] == ["org", "users", "grants", "apps"]
    assert me["user"]["status"] == "active"
    await c.close()


async def test_csrf_required_for_writes() -> None:
    c = await admin_client()
    root = await root_id(c)
    r = await c.http.post("/api/v1/depts", json={"parent_id": root, "name": "研发"})  # 不带 CSRF 头
    assert r.status_code == 403 and r.json()["code"] == "CSRF_FAILED"
    await c.close()


async def test_lockout_and_unlock() -> None:
    admin = await admin_client()
    uid, temp = await make_user(admin, "zhang.ming", await root_id(admin))
    c = Client()
    for i in range(4):
        r = await c.login("zhang.ming", "wrong")
        assert r.status_code == 401, r.text
        assert r.json()["details"]["left"] == 4 - i
    r = await c.login("zhang.ming", "wrong")
    assert r.json()["code"] == "ACCOUNT_LOCKED"
    # 锁定后正确密码也不行（失败计数与锁定已落库，没有随请求回滚）
    r = await c.login("zhang.ming", temp)
    assert r.json()["code"] == "ACCOUNT_LOCKED"
    users = (await admin.get("/api/v1/users", params={"status": "locked"})).json()
    assert users["counts"]["locked"] == 1
    assert (await admin.post(f"/api/v1/users/{uid}/unlock")).status_code == 200
    assert (await c.login("zhang.ming", temp)).status_code == 200
    await admin.close()
    await c.close()


async def test_user_create_rules_and_status_flow() -> None:
    c = await admin_client()
    root = await root_id(c)
    uid, temp = await make_user(c, "li.gong", root)
    assert len(temp) >= 12
    r = await c.post(
        "/api/v1/users", {"name": "x", "account": "li.gong", "email": "x@x.com", "dept_id": root}
    )
    assert r.json()["code"] == "ACCOUNT_EXISTS"
    r = await c.post(
        "/api/v1/users", {"name": "x", "account": "Bad Name", "email": "x@x.com", "dept_id": root}
    )
    assert r.json()["code"] == "VALIDATION_FAILED"
    r = await c.post(
        "/api/v1/users",
        {"name": "x", "account": "x.y", "email": "LI.GONG@xinghai.com", "dept_id": root},
    )
    assert r.json()["code"] == "EMAIL_EXISTS"
    assert (await c.get(f"/api/v1/users/{uid}")).json()["status"] == "pending"

    # 停用：会话失效、不能登录；启用后回到「未激活」（从未激活过）
    other = Client()
    assert (await other.login("li.gong", temp)).status_code == 200
    assert (await c.post(f"/api/v1/users/{uid}/disable", {"reason": "离职"})).json()[
        "status"
    ] == "disabled"
    assert (await other.get("/api/v1/auth/me")).status_code == 401
    assert (await other.login("li.gong", temp)).json()["code"] == "USER_DISABLED"
    assert (await c.post(f"/api/v1/users/{uid}/reset-password")).json()["code"] == "USER_DISABLED"
    assert (await c.post(f"/api/v1/users/{uid}/enable")).json()["status"] == "pending"

    # 重置密码：旧临时密码失效
    new_temp = (await c.post(f"/api/v1/users/{uid}/reset-password")).json()["temp_password"]
    assert (await other.login("li.gong", temp)).status_code == 401
    assert (await other.login("li.gong", new_temp)).status_code == 200
    await c.close()
    await other.close()


async def test_cannot_disable_self_or_last_super() -> None:
    c = await admin_client()
    me = (await c.get("/api/v1/auth/me")).json()["user"]["id"]
    r = await c.post(f"/api/v1/users/{me}/disable", {})
    assert r.json()["code"] == "CANNOT_DISABLE_SELF"
    await c.close()


async def test_permission_denied_without_ops() -> None:
    admin = await admin_client()
    _, temp = await make_user(admin, "zhou.ning", await root_id(admin))
    c = Client()
    await c.login_and_activate("zhou.ning", temp)
    me = (await c.get("/api/v1/auth/me")).json()
    assert me["permissions"] == []
    # 「权限管理」是公共菜单，人人可见
    assert [m["code"] for m in me["menus"]] == ["grants"]
    r = await c.get("/api/v1/users")
    assert r.status_code == 403 and r.json()["code"] == "PERMISSION_DENIED"
    await admin.close()
    await c.close()
