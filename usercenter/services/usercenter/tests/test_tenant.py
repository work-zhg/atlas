"""租户隔离（租户设计 §15 P1 退出标准）。

两个租户各造一份数据：A 的凭据碰 B 的东西一律 404 / 被拒。
"""

from __future__ import annotations

import pytest
from atlas_usercenter.db.models import Grant, User
from atlas_usercenter.db.session import get_sessionmaker
from atlas_usercenter.db.tenancy import MissingTenant, platform_scope
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from uc_testkit import STRONG, Client, admin_client, make_user, role_id, root_id


async def _tenant(c: Client, code: str, *, max_users: int | None = None) -> tuple[str, Client]:
    """平台管理员建租户，返回 (租户 id, 已激活的租户管理员客户端)。"""
    r = await c.post(
        "/api/v1/tenants",
        {
            "code": code,
            "name": f"{code} 公司",
            "max_users": max_users,
            "admin": {"account": "boss", "name": "老板", "email": f"boss@{code}.com"},
        },
    )
    assert r.status_code == 201, r.text
    admin = Client()
    await admin.login_and_activate("boss", r.json()["temp_password"], tenant=code)
    return r.json()["tenant"]["id"], admin


async def _app(c: Client, name: str = "AI TeamFlow") -> tuple[str, str, str]:
    """平台接入一个应用（编码 teamflow）→ (应用 id, app_key, app_secret)。"""
    r = await c.post("/api/v1/apps", {"name": name, "code": "teamflow"})
    app_id, secret = r.json()["app"]["id"], r.json()["app_secret"]
    key = (await c.get(f"/api/v1/apps/{app_id}")).json()["app_key"]
    return app_id, key, secret


async def test_tenant_admin_lives_in_an_isolated_org() -> None:
    plat = await admin_client()
    _tid, acme = await _tenant(plat, "acme")

    # 各自一棵树、一个根
    acme_root = await root_id(acme)
    plat_root = await root_id(plat)
    assert acme_root != plat_root
    assert {d["name"] for d in (await acme.get("/api/v1/depts/tree")).json()} == {"acme 公司"}

    # 同名账号可以在两个租户各有一个
    a_uid, _ = await make_user(acme, "zhang.ming", acme_root)
    p_uid, _ = await make_user(plat, "zhang.ming", plat_root)
    assert a_uid != p_uid

    # 互相看不见：列表只有自己的，按 id 访问别人的一律 404
    acme_accounts = {u["account"] for u in (await acme.get("/api/v1/users")).json()["items"]}
    assert acme_accounts == {"boss", "zhang.ming"}
    assert (await acme.get(f"/api/v1/users/{p_uid}")).status_code == 404
    assert (await acme.patch(f"/api/v1/users/{p_uid}", {"name": "改"})).status_code == 404
    assert (await acme.get(f"/api/v1/depts/{plat_root}")).status_code == 404
    names = {u["account"] for u in (await acme.get("/api/v1/directory/users")).json()}
    assert "admin" not in names

    # 不能把别的租户的部门 / 人拿来用
    r = await acme.post(
        "/api/v1/users",
        {"name": "x", "account": "x.y", "email": "x@acme.com", "dept_id": plat_root},
    )
    assert r.json()["code"] == "DEPT_NOT_FOUND"
    super_role = await role_id(acme, "UC_SUPER")
    r = await acme.post(
        "/api/v1/grants",
        {"kind": "role", "target_ids": [super_role], "subjects": [{"type": "user", "id": p_uid}]},
    )
    assert r.json()["code"] == "SUBJECT_NOT_FOUND", r.text  # 在本租户里就是不存在
    await plat.close()
    await acme.close()


async def test_platform_abilities_never_reach_a_customer_tenant() -> None:
    plat = await admin_client()
    _tid, acme = await _tenant(plat, "acme")
    me = (await acme.get("/api/v1/auth/me")).json()
    # 同样是超级管理员角色，客户租户里没有平台级操作与菜单
    assert me["tenant"]["kind"] == "customer"
    assert "user:create" in me["permissions"]
    assert not {"app:manage", "tenant:manage", "tenant:view"} & set(me["permissions"])
    assert "tenants" not in {m.get("code") for m in me["menus"]}
    assert (await acme.get("/api/v1/tenants")).status_code == 403
    assert (await acme.post("/api/v1/apps", {"name": "x"})).status_code == 403
    await plat.close()
    await acme.close()


async def test_login_needs_the_company_code_once_there_are_tenants() -> None:
    plat = await admin_client()
    await _tenant(plat, "acme")
    c = Client()
    r = await c.login("admin", STRONG)
    assert r.json()["code"] == "TENANT_REQUIRED"
    # 企业代码不存在与密码错误同一句提示：不让人枚举租户
    bad_code = (await c.login("admin", STRONG, tenant="nope")).json()
    bad_pwd = (await c.login("admin", "wrong", tenant="default")).json()
    assert bad_code["code"] == bad_pwd["code"] == "LOGIN_FAILED"
    # 账号在别的租户：等同不存在
    assert (await c.login("boss", STRONG, tenant="default")).json()["code"] == "LOGIN_FAILED"
    assert (await c.login("admin", STRONG, tenant="default")).status_code == 200
    assert (await c.get("/api/v1/auth/tenant-required")).json() == {"required": True}
    await plat.close()
    await c.close()


async def test_open_api_is_scoped_to_the_header_tenant() -> None:
    """开放接口的租户 = 请求头 X-Tenant-Id；缺省按 X-Uid 所属租户推（内网身份传递 §3）。"""
    plat = await admin_client()
    acme_id, acme = await _tenant(plat, "acme")
    app_id, _key, _secret = await _app(plat)
    li, _ = await make_user(acme, "li.gong", await root_id(acme))
    h = {"X-Caller-App": "teamflow", "X-Tenant-Id": acme_id}

    # 没开通：403
    assert (await plat.http.get("/open/v1/users", headers=h)).json()["code"] == "APP_NOT_ENABLED"
    r = await plat.put(f"/api/v1/tenants/{acme_id}/apps/{app_id}", {"enabled": True})
    assert r.status_code == 200, r.text
    # 租户自己配范围（不需要平台权限）
    r = await acme.put(f"/api/v1/apps/{app_id}/scope", {"all": True, "dept_ids": []})
    assert r.status_code == 200, r.text

    users = (await plat.http.get("/open/v1/users", headers=h)).json()["items"]
    assert {u["account"] for u in users} == {"boss", "li.gong"}
    # 平台租户的人：在 acme 下不存在
    admin_id = (await plat.get("/api/v1/auth/me")).json()["user"]["id"]
    assert (await plat.http.get(f"/open/v1/users/{admin_id}", headers=h)).status_code == 404
    # 不带租户：按 X-Uid 推（后台任务只记得 uid）；都没有且多租户 → 400
    by_uid = {"X-Caller-App": "teamflow", "X-Uid": li}
    users = (await plat.http.get("/open/v1/users", headers=by_uid)).json()["items"]
    assert {u["account"] for u in users} == {"boss", "li.gong"}
    r = await plat.http.get("/open/v1/users", headers={"X-Caller-App": "teamflow"})
    assert r.status_code == 400 and r.json()["code"] == "MISSING_TENANT"
    tenants = (await plat.http.get("/open/v1/tenants", headers=h)).json()
    assert {t["code"] for t in tenants} == {"default", "acme"}

    # 数据权限：两个租户的同一个 data_id 互不相干
    await plat.post(f"/api/v1/apps/{app_id}/data-types", {"code": "Team", "name": "团队"})
    boss = next(u["id"] for u in users if u["account"] == "boss")
    r = await plat.http.post(
        "/open/v1/data-permissions",
        json={
            "data_code": "Team",
            "data_id": "t1",
            "permission": "OWNER",
            "subject_type": "USER",
            "subject_id": boss,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    plat_tenant = (await plat.get("/api/v1/auth/me")).json()["tenant"]["id"]
    ph = {"X-Caller-App": "teamflow", "X-Tenant-Id": plat_tenant}
    r = await plat.http.get(
        "/open/v1/data-permissions/acl", params={"data_code": "Team", "data_id": "t1"}, headers=ph
    )
    assert r.json()["code"] == "DATA_NOT_FOUND"

    # 租户停用：登录与开放接口立即失效
    r = await plat.patch(f"/api/v1/tenants/{acme_id}", {"status": "suspended"})
    assert r.status_code == 200, r.text
    assert (await plat.http.get("/open/v1/users", headers=h)).json()["code"] == "TENANT_DISABLED"
    assert (await acme.get("/api/v1/auth/me")).json()["code"] == "TENANT_DISABLED"
    await plat.close()
    await acme.close()


async def test_roles_are_shared_or_owned_by_a_tenant() -> None:
    plat = await admin_client()
    acme_id, acme = await _tenant(plat, "acme")
    other_id, other = await _tenant(plat, "other")
    app_id, _key, _secret = await _app(plat)
    for tid in (acme_id, other_id):
        await plat.put(f"/api/v1/tenants/{tid}/apps/{app_id}", {"enabled": True})

    shared = await plat.post(f"/api/v1/apps/{app_id}/roles", {"name": "成员", "code": "TF_MEMBER"})
    assert shared.status_code == 201, shared.text
    own = await acme.post(
        f"/api/v1/apps/{app_id}/roles", {"name": "合同管理", "code": "ACME_CONTRACT"}
    )
    assert own.status_code == 201, own.text

    acme_roles = {r["code"] for r in (await acme.get(f"/api/v1/apps/{app_id}/roles")).json()}
    other_roles = {r["code"] for r in (await other.get(f"/api/v1/apps/{app_id}/roles")).json()}
    assert acme_roles == {"TF_MEMBER", "ACME_CONTRACT"}
    assert other_roles == {"TF_MEMBER"}, "别的租户的自定义角色不可见"
    # 应用内置（共用）角色只有平台能改
    r = await acme.patch(f"/api/v1/roles/{shared.json()['id']}", {"name": "改名"})
    assert r.json()["code"] == "SHARED_ROLE_READONLY"
    assert (await other.get(f"/api/v1/roles/{own.json()['id']}")).status_code == 404
    for c in (plat, acme, other):
        await c.close()


async def test_tenant_quota() -> None:
    plat = await admin_client()
    _tid, acme = await _tenant(plat, "acme", max_users=2)
    root = await root_id(acme)
    await make_user(acme, "one", root)
    r = await acme.post(
        "/api/v1/users", {"name": "two", "account": "two", "email": "two@acme.com", "dept_id": root}
    )
    assert r.json()["code"] == "TENANT_QUOTA_EXCEEDED"
    await plat.close()
    await acme.close()


async def test_database_refuses_cross_tenant_references() -> None:
    """复合外键：即使绕过服务层，跨租户的引用也写不进去。"""
    plat = await admin_client()
    _tid, acme = await _tenant(plat, "acme")
    await acme.close()
    await plat.close()
    async with get_sessionmaker()() as session:
        with pytest.raises(MissingTenant):  # 没有租户上下文不许查租户表
            await session.execute(select(User))
        with platform_scope(session):
            boss = (await session.execute(select(User).where(User.account == "boss"))).scalar_one()
            admin = (
                await session.execute(select(User).where(User.account == "admin"))
            ).scalar_one()
            role = await session.scalar(
                select(Grant.role_uuid).where(Grant.user_uuid == admin.uuid).limit(1)
            )
            # acme 的授权行指向平台租户的人
            session.add(
                Grant(
                    tenant_uuid=boss.tenant_uuid,
                    kind="role",
                    role_uuid=role,
                    subject_type="user",
                    user_uuid=admin.uuid,
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
        await session.rollback()
