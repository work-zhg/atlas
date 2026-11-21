"""租户隔离（租户设计 §15 P1 退出标准）与运营接口（租户隔离设计 §3、§10）。

两个租户各造一份数据：A 的凭据碰 B 的东西一律 404 / 被拒。没有平台租户：租户和应用目录
只有运营中心经运营接口（签名）能管，管的时候看不到租户里的人、授权和自定义角色。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from atlas_usercenter.db.models import Base, Grant, Role, Tenant, TenantOwned, User
from atlas_usercenter.db.session import get_sessionmaker
from atlas_usercenter.db.tenancy import (
    MissingTenant,
    catalog_scope,
    platform_scope,
    tenant_scope,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from uc_testkit import (
    STRONG,
    Client,
    OpsClient,
    admin_client,
    make_app,
    make_tenant,
    make_user,
    role_id,
    root_id,
)


async def test_tenant_admin_lives_in_an_isolated_org() -> None:
    ops = OpsClient()
    dflt = await admin_client()
    _tid, acme = await make_tenant(ops, "acme")

    # 各自一棵树、一个根
    acme_root = await root_id(acme)
    dflt_root = await root_id(dflt)
    assert acme_root != dflt_root
    assert {d["name"] for d in (await acme.get("/api/v1/depts/tree")).json()} == {"acme 公司"}

    # 同名账号可以在两个租户各有一个
    a_uid, _ = await make_user(acme, "zhang.ming", acme_root)
    p_uid, _ = await make_user(dflt, "zhang.ming", dflt_root)
    assert a_uid != p_uid

    # 互相看不见：列表只有自己的，按 id 访问别人的一律 404
    acme_accounts = {u["account"] for u in (await acme.get("/api/v1/users")).json()["items"]}
    assert acme_accounts == {"boss", "zhang.ming"}
    assert (await acme.get(f"/api/v1/users/{p_uid}")).status_code == 404
    assert (await acme.patch(f"/api/v1/users/{p_uid}", {"name": "改"})).status_code == 404
    assert (await acme.get(f"/api/v1/depts/{dflt_root}")).status_code == 404
    names = {u["account"] for u in (await acme.get("/api/v1/directory/users")).json()}
    assert "admin" not in names

    # 不能把别的租户的部门 / 人拿来用
    r = await acme.post(
        "/api/v1/users",
        {"name": "x", "account": "x.y", "email": "x@acme.com", "dept_id": dflt_root},
    )
    assert r.json()["code"] == "DEPT_NOT_FOUND"
    super_role = await role_id(acme, "UC_SUPER")
    r = await acme.post(
        "/api/v1/grants",
        {"kind": "role", "target_ids": [super_role], "subjects": [{"type": "user", "id": p_uid}]},
    )
    assert r.json()["code"] == "SUBJECT_NOT_FOUND", r.text  # 在本租户里就是不存在
    for c in (ops, dflt, acme):
        await c.close()


async def test_no_tenant_holds_ops_abilities() -> None:
    """所有租户一律平等：哪个租户的超级管理员都没有租户、应用目录的能力；运营接口只认签名。"""
    ops = OpsClient()
    _tid, acme = await make_tenant(ops, "acme")
    dflt = await admin_client()
    for c in (dflt, acme):
        me = (await c.get("/api/v1/auth/me")).json()
        assert "user:create" in me["permissions"]
        assert not {"app:manage", "tenant:manage", "tenant:view"} & set(me["permissions"])
        assert "tenants" not in {m.get("code") for m in me["menus"]}
        assert (await c.get("/api/v1/tenants")).status_code == 404
        # 登录态、内网请求头都进不了运营接口
        assert (await c.get("/ops/v1/tenants")).json()["code"] == "OPS_NOT_ALLOWED"
        r = await c.http.get("/ops/v1/apps", headers={"X-Caller-App": "atlas"})
        assert r.status_code == 401
    # 别的密钥签的不认
    wrong = OpsClient(key="not-the-key")
    assert (await wrong.get("/ops/v1/tenants")).status_code == 401
    for c in (ops, dflt, acme, wrong):
        await c.close()


async def test_login_needs_the_company_code_once_there_are_tenants() -> None:
    dflt = await admin_client()
    # 只有一个租户：不要企业代码，登录页展示企业名
    sole = (await dflt.get("/api/v1/auth/tenant-required")).json()
    dflt_name = (await dflt.get("/api/v1/auth/me")).json()["tenant"]["name"]
    assert sole == {"required": False, "tenant": {"name": dflt_name}}
    ops = OpsClient()
    await make_tenant(ops, "acme")
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
    # 多租户：不透露有哪些企业
    multi = (await c.get("/api/v1/auth/tenant-required")).json()
    assert multi == {"required": True, "tenant": None}
    for x in (dflt, ops, c):
        await x.close()


async def test_open_api_is_scoped_to_the_header_tenant() -> None:
    """开放接口的租户 = 请求头 X-Tenant-Id；缺省按 X-Uid 所属租户推（内网身份传递 §3）。"""
    ops = OpsClient()
    dflt = await admin_client()
    acme_id, acme = await make_tenant(ops, "acme")
    app_id = (await make_app("AI TeamFlow", "teamflow", tenants=()))["app"]["id"]
    li, _ = await make_user(acme, "li.gong", await root_id(acme))
    h = {"X-Caller-App": "teamflow", "X-Tenant-Id": acme_id}

    # 没开通：403
    assert (await dflt.http.get("/open/v1/users", headers=h)).json()["code"] == "APP_NOT_ENABLED"
    r = await ops.put(f"/ops/v1/tenants/{acme_id}/apps/{app_id}", {"enabled": True})
    assert r.status_code == 200, r.text
    apps = (await ops.get(f"/ops/v1/tenants/{acme_id}/apps")).json()
    assert [a["enabled"] for a in apps if a["app"]["id"] == app_id] == [True]
    # 运营中心的应用目录看得到几个租户开通了它；租户管理员看不到（别的租户的事）
    catalog = {a["id"]: a for a in (await ops.get("/ops/v1/apps")).json()}
    assert catalog[app_id]["tenants"] == 1
    assert (await ops.get(f"/ops/v1/apps/{app_id}")).json()["tenants"] == 1
    assert "tenants" not in (await acme.get(f"/api/v1/apps/{app_id}")).json()
    # 租户自己配范围（不需要运营中心）
    r = await acme.put(f"/api/v1/apps/{app_id}/scope", {"all": True, "dept_ids": []})
    assert r.status_code == 200, r.text
    # 没给 default 开通：default 的管理台里看不到这个应用
    assert app_id not in {a["id"] for a in (await dflt.get("/api/v1/apps")).json()}

    users = (await dflt.http.get("/open/v1/users", headers=h)).json()["items"]
    assert {u["account"] for u in users} == {"boss", "li.gong"}
    # default 租户的人：在 acme 下不存在
    admin_id = (await dflt.get("/api/v1/auth/me")).json()["user"]["id"]
    assert (await dflt.http.get(f"/open/v1/users/{admin_id}", headers=h)).status_code == 404
    # 不带租户：按 X-Uid 推（后台任务只记得 uid）；都没有且多租户 → 400
    by_uid = {"X-Caller-App": "teamflow", "X-Uid": li}
    users = (await dflt.http.get("/open/v1/users", headers=by_uid)).json()["items"]
    assert {u["account"] for u in users} == {"boss", "li.gong"}
    r = await dflt.http.get("/open/v1/users", headers={"X-Caller-App": "teamflow"})
    assert r.status_code == 400 and r.json()["code"] == "MISSING_TENANT"
    tenants = (await dflt.http.get("/open/v1/tenants", headers=h)).json()
    assert {t["code"] for t in tenants} == {"acme"}

    # 数据权限：两个租户的同一个 data_id 互不相干
    await ops.post(f"/ops/v1/apps/{app_id}/data-types", {"code": "Team", "name": "团队"})
    dflt_id = await ops.tenant_id("default")
    await ops.put(f"/ops/v1/tenants/{dflt_id}/apps/{app_id}", {"enabled": True})
    boss = next(u["id"] for u in users if u["account"] == "boss")
    r = await dflt.http.post(
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
    dflt_tenant = (await dflt.get("/api/v1/auth/me")).json()["tenant"]["id"]
    ph = {"X-Caller-App": "teamflow", "X-Tenant-Id": dflt_tenant}
    r = await dflt.http.get(
        "/open/v1/data-permissions/acl", params={"data_code": "Team", "data_id": "t1"}, headers=ph
    )
    assert r.json()["code"] == "DATA_NOT_FOUND"

    # 运营中心挡住入口：登录与开放接口立即失效
    r = await ops.post(f"/ops/v1/tenants/{acme_id}/block")
    assert r.status_code == 200 and r.json()["status"] == "suspended", r.text
    assert (await dflt.http.get("/open/v1/users", headers=h)).json()["code"] == "TENANT_DISABLED"
    assert (await acme.get("/api/v1/auth/me")).json()["code"] == "TENANT_DISABLED"
    # 放开后恢复
    assert (await ops.post(f"/ops/v1/tenants/{acme_id}/unblock")).json()["status"] == "active"
    assert (await dflt.http.get("/open/v1/users", headers=h)).status_code == 200
    for c in (ops, dflt, acme):
        await c.close()


async def test_roles_are_shared_or_owned_by_a_tenant() -> None:
    ops = OpsClient()
    _acme_id, acme = await make_tenant(ops, "acme")
    _other_id, other = await make_tenant(ops, "other")
    app_id = (await make_app("AI TeamFlow", "teamflow", tenants=("acme", "other")))["app"]["id"]

    shared = await ops.post(f"/ops/v1/apps/{app_id}/roles", {"name": "成员", "code": "TF_MEMBER"})
    assert shared.status_code == 201, shared.text
    assert shared.json()["shared"] is True
    own = await acme.post(
        f"/api/v1/apps/{app_id}/roles", {"name": "合同管理", "code": "ACME_CONTRACT"}
    )
    assert own.status_code == 201, own.text
    assert own.json()["shared"] is False

    acme_roles = {r["code"] for r in (await acme.get(f"/api/v1/apps/{app_id}/roles")).json()}
    other_roles = {r["code"] for r in (await other.get(f"/api/v1/apps/{app_id}/roles")).json()}
    assert acme_roles == {"TF_MEMBER", "ACME_CONTRACT"}
    assert other_roles == {"TF_MEMBER"}, "别的租户的自定义角色不可见"
    # 应用内置（共用）角色只有运营中心能改
    r = await acme.patch(f"/api/v1/roles/{shared.json()['id']}", {"name": "改名"})
    assert r.json()["code"] == "SHARED_ROLE_READONLY"
    r = await ops.patch(f"/ops/v1/roles/{shared.json()['id']}", {"name": "团队成员"})
    assert r.status_code == 200 and r.json()["name"] == "团队成员"
    assert (await other.get(f"/api/v1/roles/{own.json()['id']}")).status_code == 404
    # 运营中心也看不到租户自定义的角色
    ops_roles = {r["code"] for r in (await ops.get(f"/ops/v1/apps/{app_id}/roles")).json()}
    assert ops_roles == {"TF_MEMBER"}
    assert (await ops.get(f"/ops/v1/roles/{own.json()['id']}")).status_code == 404
    assert (await ops.delete(f"/ops/v1/roles/{own.json()['id']}")).status_code == 404
    for c in (ops, acme, other):
        await c.close()


async def test_catalog_shows_numbers_not_tenant_content() -> None:
    """删操作码 / 角色时运营中心看到的影响面：只有应用内置角色的名字和数字。"""
    ops = OpsClient()
    _tid, acme = await make_tenant(ops, "acme")
    app_id = (await make_app("AI TeamFlow", "teamflow", tenants=("acme",)))["app"]["id"]
    op = (
        await ops.post(
            f"/ops/v1/apps/{app_id}/operations",
            {"module": "团队", "code": "team:view", "name": "看"},
        )
    ).json()["id"]
    shared = (
        await ops.post(f"/ops/v1/apps/{app_id}/roles", {"name": "成员", "code": "TF_MEMBER"})
    ).json()["id"]
    await ops.put(f"/ops/v1/roles/{shared}/operations", {"ids": [op]})
    secret = (
        await acme.post(f"/api/v1/apps/{app_id}/roles", {"name": "并购小组", "code": "ACME_MA"})
    ).json()["id"]
    assert (await acme.put(f"/api/v1/roles/{secret}/operations", {"ids": [op]})).status_code == 200
    boss = (await acme.get("/api/v1/auth/me")).json()["user"]["id"]
    r = await acme.post(
        "/api/v1/grants",
        {"kind": "role", "target_ids": [shared], "subjects": [{"type": "user", "id": boss}]},
    )
    assert r.status_code == 201, r.text

    listed = (await ops.get(f"/ops/v1/apps/{app_id}/operations")).json()
    assert listed[0]["roles"] == ["成员"]
    impact = (await ops.delete(f"/ops/v1/operations/{op}", params={"dry_run": "true"})).json()
    assert impact == {"roles": ["成员"]}
    impact = (await ops.delete(f"/ops/v1/roles/{shared}", params={"dry_run": "true"})).json()
    assert impact == {"grants": 1, "positions": 0}
    # 真删：各租户里这个角色的授权一并删掉
    assert (await ops.delete(f"/ops/v1/roles/{shared}")).status_code == 200
    assert (await acme.get("/api/v1/auth/me")).status_code == 200
    for c in (ops, acme):
        await c.close()


async def test_create_tenant_is_idempotent_and_reissues_until_admin_logs_in() -> None:
    ops = OpsClient()
    tid = str(uuid4())
    body = {
        "uuid": tid,
        "code": "xinghe",
        "name": "星河科技",
        "max_users": 10,
        "expires_at": (datetime.now(UTC) + timedelta(days=365)).isoformat(),
        "admin": {"account": "wangwu", "name": "王五", "email": "wangwu@xinghe.example"},
    }
    first = await ops.post("/ops/v1/tenants", body)
    assert first.status_code == 201, first.text
    t1 = first.json()["temp_password"]
    # 超时重做：同一个 uuid → 200，不再建一个；首个管理员没登录过，重发临时密码（旧的作废）
    again = await ops.post("/ops/v1/tenants", body)
    assert again.status_code == 200, again.text
    t2 = again.json()["temp_password"]
    assert t2 and t2 != t1
    assert [t["code"] for t in (await ops.get("/ops/v1/tenants")).json()].count("xinghe") == 1
    c = Client()
    assert (await c.login("wangwu", t1, tenant="xinghe")).status_code == 401
    # 补发也一样
    t3 = (await ops.post(f"/ops/v1/tenants/{tid}/admin-password")).json()["temp_password"]
    await c.login_and_activate("wangwu", t3, tenant="xinghe")
    # 登录过之后，运营中心再也拿不到这个账号
    r = await ops.post(f"/ops/v1/tenants/{tid}/admin-password")
    assert r.status_code == 409 and r.json()["code"] == "ADMIN_ACTIVATED"
    assert (await ops.post("/ops/v1/tenants", body)).json()["temp_password"] is None
    # 同一个 uuid 换了企业代码、或企业代码被占用：拒绝
    r = await ops.post("/ops/v1/tenants", {**body, "code": "other"})
    assert r.json()["code"] == "TENANT_UUID_CONFLICT"
    r = await ops.post("/ops/v1/tenants", {**body, "uuid": str(uuid4())})
    assert r.json()["code"] == "TENANT_CODE_EXISTS"
    # 改名字、名额、到期
    r = await ops.patch(f"/ops/v1/tenants/{tid}", {"name": "星河科技集团", "max_users": 20})
    assert r.json()["name"] == "星河科技集团" and r.json()["max_users"] == 20
    detail = (await ops.get(f"/ops/v1/tenants/{tid}")).json()
    assert detail["users"] == 1 and detail["usable"] is True
    for x in (ops, c):
        await x.close()


async def test_expired_tenants_are_listed_for_the_ops_center() -> None:
    ops = OpsClient()
    acme_id, acme = await make_tenant(ops, "acme")
    assert (await ops.get("/ops/v1/tenants/expired")).json() == []
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await ops.patch(f"/ops/v1/tenants/{acme_id}", {"expires_at": past})
    assert [t["id"] for t in (await ops.get("/ops/v1/tenants/expired")).json()] == [acme_id]
    # 到期本身就挡住登录；运营中心再走停用流程，停用之后就不在这个列表里了
    assert (await acme.get("/api/v1/auth/me")).json()["code"] == "TENANT_DISABLED"
    await ops.post(f"/ops/v1/tenants/{acme_id}/block")
    assert (await ops.get("/ops/v1/tenants/expired")).json() == []
    for c in (ops, acme):
        await c.close()


async def test_delete_tenant_removes_all_its_data() -> None:
    ops = OpsClient()
    dflt = await admin_client()
    acme_id, acme = await make_tenant(ops, "acme")
    app_id = (await make_app("AI TeamFlow", "teamflow", tenants=("acme", "default")))["app"]["id"]
    await ops.post(f"/ops/v1/apps/{app_id}/data-types", {"code": "Team", "name": "团队"})
    root = await root_id(acme)
    li, _ = await make_user(acme, "li.gong", root)
    await acme.put(f"/api/v1/apps/{app_id}/scope", {"all": True, "dept_ids": []})
    role = (
        await acme.post(f"/api/v1/apps/{app_id}/roles", {"name": "合同管理", "code": "ACME_C"})
    ).json()["id"]
    await acme.post(
        "/api/v1/grants",
        {"kind": "role", "target_ids": [role], "subjects": [{"type": "dept", "id": root}]},
    )
    await acme.post("/api/v1/positions", {"code": "LEAD", "name": "负责人", "role_ids": [role]})
    h = {"X-Caller-App": "teamflow", "X-Tenant-Id": acme_id}
    await acme.http.post(
        "/open/v1/digital-employees",
        json={"external_ref": "agent-1", "name": "助手", "owner_id": li},
        headers=h,
    )
    r = await acme.http.post(
        "/open/v1/data-permissions",
        json={
            "data_code": "Team",
            "data_id": "t1",
            "permission": "OWNER",
            "subject_type": "USER",
            "subject_id": li,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    dflt_users = len((await dflt.get("/api/v1/users")).json()["items"])

    # 没停用不能删
    r = await ops.delete(f"/ops/v1/tenants/{acme_id}")
    assert r.status_code == 409 and r.json()["code"] == "TENANT_ACTIVE"
    await ops.post(f"/ops/v1/tenants/{acme_id}/block")
    r = await ops.delete(f"/ops/v1/tenants/{acme_id}")
    assert r.status_code == 200, r.text
    deleted = r.json()["deleted"]
    assert deleted["uc_user"] == 3  # boss、李工、数字员工
    assert deleted["uc_role"] == 1 and deleted["uc_data_acl"] == 1
    # 幂等：再删一次也成功
    assert (await ops.delete(f"/ops/v1/tenants/{acme_id}")).json()["deleted"] == {}
    assert (await ops.get(f"/ops/v1/tenants/{acme_id}")).status_code == 404

    async with get_sessionmaker()() as session:
        with platform_scope(session):
            for mapper in Base.registry.mappers:
                cls = mapper.class_
                if issubclass(cls, TenantOwned) or cls is Role:
                    n = await session.scalar(
                        select(func.count()).select_from(cls).where(cls.tenant_uuid == acme_id)
                    )
                    assert n == 0, cls.__name__
            assert await session.scalar(select(Tenant.uuid).where(Tenant.code == "acme")) is None
    # 别的租户不受影响
    assert len((await dflt.get("/api/v1/users")).json()["items"]) == dflt_users
    for c in (ops, dflt, acme):
        await c.close()


async def test_tenant_quota() -> None:
    ops = OpsClient()
    _tid, acme = await make_tenant(ops, "acme", max_users=2)
    root = await root_id(acme)
    await make_user(acme, "one", root)
    r = await acme.post(
        "/api/v1/users", {"name": "two", "account": "two", "email": "two@acme.com", "dept_id": root}
    )
    assert r.json()["code"] == "TENANT_QUOTA_EXCEEDED"
    await ops.close()
    await acme.close()


async def test_count_and_join_only_queries_are_filtered_too() -> None:
    """select(func.count()).select_from(租户表)、只在 join 里出现的租户表：同样按租户过滤；
    没有上下文同样报错（以前 with_loader_criteria 管不到它们，会数出全部租户）。"""
    ops = OpsClient()
    _tid, acme = await make_tenant(ops, "acme")
    await make_user(acme, "li.gong", await root_id(acme))
    await ops.close()
    await acme.close()
    async with get_sessionmaker()() as session:
        with pytest.raises(MissingTenant):
            await session.execute(select(func.count()).select_from(User))
        with platform_scope(session):
            total = await session.scalar(select(func.count()).select_from(User))
            acme_uuid = await session.scalar(select(Tenant.uuid).where(Tenant.code == "acme"))
        with tenant_scope(session, acme_uuid):
            assert await session.scalar(select(func.count()).select_from(User)) == 2
            joined = (
                select(func.count(Grant.id))
                .select_from(Grant)
                .join(User, User.uuid == Grant.user_uuid)
            )
            assert await session.scalar(joined) == 1  # 只有 boss 的超级管理员授权
        assert total == 3  # default 的 admin + acme 的两个人
        # 应用目录范围：只有应用内置角色，碰租户表就报错
        with catalog_scope(session):
            codes = set(await session.scalars(select(Role.code)))
            assert "UC_SUPER" in codes
            with pytest.raises(MissingTenant):
                await session.execute(select(func.count()).select_from(Grant))


async def test_database_refuses_cross_tenant_references() -> None:
    """复合外键：即使绕过服务层，跨租户的引用也写不进去。"""
    ops = OpsClient()
    _tid, acme = await make_tenant(ops, "acme")
    await acme.close()
    await ops.close()
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
            # acme 的授权行指向 default 租户的人
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
