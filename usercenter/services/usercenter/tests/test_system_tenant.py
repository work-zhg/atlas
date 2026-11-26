"""系统租户与「仅系统租户」的应用（c4-model/opscenter/design-opscenter.html）。"""

from __future__ import annotations

from atlas_usercenter.bootstrap import bootstrap_system
from atlas_usercenter.db.models import TenantApp
from atlas_usercenter.db.session import get_sessionmaker
from atlas_usercenter.db.tenancy import tenant_scope
from atlas_usercenter.errors import Conflict
from uc_testkit import Client, OpsClient, make_tenant

OPSCENTER = {
    "name": "运营中心",
    "description": "管平台本身",
    "home_url": "/opscenter-ui/",
    "system_only": True,
    "operations": [{"module": "运营", "code": "ops:audit", "name": "查看运营审计"}],
    "data_types": [
        {"code": "OpsTenant", "name": "租户运营"},
        {"code": "OpsMcp", "name": "MCP 网关运营"},
    ],
    "roles": [{"code": "OPS_AUDITOR", "name": "运营审计员", "operations": ["ops:audit"]}],
}


async def _system_tenant() -> tuple[str, Client]:
    async with get_sessionmaker()() as s:
        _account, temp = await bootstrap_system(
            s,
            code="platform",
            company="平台运营",
            account="ops",
            name="运营负责人",
            email="o@p.com",
        )
        await s.commit()
    boss = Client()
    await boss.login_and_activate("ops", temp, tenant="platform")
    return temp, boss


async def test_only_one_system_tenant() -> None:
    await _system_tenant()
    async with get_sessionmaker()() as s:
        try:
            await bootstrap_system(
                s, code="platform2", company="x", account="a", name="a", email="a@a.com"
            )
            raise AssertionError("应该拒绝")
        except Conflict as exc:
            assert exc.code == "SYSTEM_TENANT_EXISTS"


async def test_system_tenant_cannot_be_blocked_deleted_or_expire() -> None:
    _, boss = await _system_tenant()
    ops = OpsClient()
    sys_id = await ops.tenant_id("platform")
    rows = {t["code"]: t for t in (await ops.get("/ops/v1/tenants")).json()}
    assert rows["platform"]["is_system"] is True and rows["default"]["is_system"] is False
    for r in (
        await ops.post(f"/ops/v1/tenants/{sys_id}/block"),
        await ops.delete(f"/ops/v1/tenants/{sys_id}"),
        await ops.patch(f"/ops/v1/tenants/{sys_id}", {"expires_at": "2030-01-01T00:00:00Z"}),
    ):
        assert (r.status_code, r.json()["code"]) == (409, "SYSTEM_TENANT"), r.text
    # 改名照常
    assert (await ops.patch(f"/ops/v1/tenants/{sys_id}", {"name": "平台运营部"})).status_code == 200
    await ops.close()
    await boss.close()


async def test_catalog_sync_is_idempotent() -> None:
    ops = OpsClient()
    r = await ops.put("/ops/v1/catalog/apps/opscenter", OPSCENTER)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["system_only"] is True and first["code"] == "opscenter"
    again = (await ops.put("/ops/v1/catalog/apps/opscenter", OPSCENTER)).json()
    assert again["id"] == first["id"] and again["version"] == first["version"]
    # 加一个模块、改个名字：只增只改
    more = {
        **OPSCENTER,
        "data_types": [*OPSCENTER["data_types"], {"code": "OpsModel", "name": "模型平台运营"}],
        "roles": [{"code": "OPS_AUDITOR", "name": "审计员", "operations": ["ops:audit"]}],
    }
    assert (await ops.put("/ops/v1/catalog/apps/opscenter", more)).status_code == 200
    dts = (await ops.get(f"/ops/v1/apps/{first['id']}/data-types")).json()
    assert sorted(d["code"] for d in dts) == ["OpsMcp", "OpsModel", "OpsTenant"]
    roles = (await ops.get(f"/ops/v1/apps/{first['id']}/roles")).json()
    assert [r["name"] for r in roles if r["code"] == "OPS_AUDITOR"] == ["审计员"]
    await ops.close()


async def test_system_only_app_only_for_system_tenant() -> None:
    _, sys_boss = await _system_tenant()
    ops = OpsClient()
    app = (await ops.put("/ops/v1/catalog/apps/opscenter", OPSCENTER)).json()
    acme_id, acme_boss = await make_tenant(ops, "acme")
    r = await ops.put(f"/ops/v1/tenants/{acme_id}/apps/{app['id']}", {"enabled": True})
    assert (r.status_code, r.json()["code"]) == (409, "SYSTEM_ONLY_APP")
    sys_id = await ops.tenant_id("platform")
    r = await ops.put(
        f"/ops/v1/tenants/{sys_id}/apps/{app['id']}", {"enabled": True, "scope_all": True}
    )
    assert r.status_code == 200, r.text

    async def snapshot(boss: Client) -> dict:
        me = (await boss.get("/api/v1/auth/me")).json()
        r = await boss.http.get(
            f"/open/v1/users/{me['user']['id']}/authz",
            headers={"X-Caller-App": "opscenter", "X-Tenant-Id": me["tenant"]["id"]},
        )
        return {"status": r.status_code, **r.json()}

    assert (await snapshot(sys_boss))["can_access"] is True
    # 就算普通租户里硬塞了一条开通记录，也不可访问
    async with get_sessionmaker()() as s:
        from uuid import UUID

        with tenant_scope(s, UUID(acme_id)):
            s.add(TenantApp(app_uuid=UUID(app["id"]), scope_all=True))
            await s.commit()
    snap = await snapshot(acme_boss)
    assert snap["status"] == 403 or snap.get("can_access") is False, snap
    for x in (ops, sys_boss, acme_boss):
        await x.close()
