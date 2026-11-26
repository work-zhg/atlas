"""多租户：技能归租户、技能市场、运营接口（停用 / 恢复 / 删除租户）。

c4-model/opscenter/design-tenant-isolation.html §8；技能市场是用户 2026-11-19 定的。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
from atlas_skill.api.app import create_app
from atlas_skill.check.package import RawPackage
from atlas_skill.db.session import get_sessionmaker
from atlas_skill.errors import Conflict
from atlas_skill.market.service import MarketService
from atlas_skill.store.layout import legacy_prefix
from atlas_skill.versions.service import SkillService
from atlas_tenancy import TenantScope
from conftest import BLOCKED_TENANTS
from skill_testkit import OTHER_TENANT, TEST_TENANT, make_zip, ops_signed, skill_files


def _pkg(**kw) -> RawPackage:  # type: ignore[no-untyped-def]
    return RawPackage(files=skill_files(**kw))


async def _release(tenant: str, pkg: RawPackage, storage, settings) -> None:  # type: ignore[no-untyped-def]
    with TenantScope.enter(UUID(tenant)):
        async with get_sessionmaker()() as s:
            svc = SkillService(s, storage, settings)
            row = await svc.ingest(pkg, source="tenant", actor="a")
            await svc.review(row.uuid, reviewer="reviewer", approve=True, note="ok")
            await svc.commit()


async def _publish_market(storage, settings, *, default: bool = False, **kw) -> int:  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as s:
        row = await MarketService(s, storage, settings).publish(
            _pkg(name="browser", **kw), actor="ops", install_by_default=default
        )
        await s.commit()
        return row.version


@pytest.fixture
async def client(storage, uc) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    app = create_app(storage=storage, uc=uc)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://skill",
        headers={"X-Uid": "alice", "X-Tenant-Id": TEST_TENANT},
    ) as c:
        yield c


async def _ops(client: httpx.AsyncClient, method: str, path: str, **kw) -> httpx.Response:  # type: ignore[no-untyped-def]
    return await client.send(ops_signed(client.build_request(method, path, **kw)))


# ───────────────────────────────────────────── 技能归租户


async def test_same_slug_in_two_tenants_is_two_skills(storage, settings) -> None:  # type: ignore[no-untyped-def]
    await _release(TEST_TENANT, _pkg(body="A 的\n"), storage, settings)
    await _release(OTHER_TENANT, _pkg(body="B 的\n"), storage, settings)

    for tenant in (TEST_TENANT, OTHER_TENANT):
        with TenantScope.enter(UUID(tenant)):
            async with get_sessionmaker()() as s:
                svc = SkillService(s, storage, settings)
                assert [(c.slug, c.latest) for c in await svc.catalog()] == [("demo", 1)]
                version = await svc.runtime_version("demo", 1)
                assert version.storage_key == f"{tenant}/_skills/demo/1/"
    assert storage.get(f"{TEST_TENANT}/_skills/demo/1/SKILL.md").endswith("A 的\n".encode())
    assert storage.get(f"{OTHER_TENANT}/_skills/demo/1/SKILL.md").endswith("B 的\n".encode())


async def test_runtime_sees_only_its_tenant(client: httpx.AsyncClient, storage, settings) -> None:  # type: ignore[no-untyped-def]
    await _release(OTHER_TENANT, _pkg(), storage, settings)
    assert (await client.get("/internal/skills/catalog")).json() == []
    assert (await client.get("/internal/skills/demo/versions/1")).status_code == 404
    other = {"X-Tenant-Id": OTHER_TENANT}
    assert (await client.get("/internal/skills/demo/versions/1", headers=other)).status_code == 200


# ───────────────────────────────────────────── 技能市场


async def test_install_copies_market_version_into_tenant(storage, settings) -> None:  # type: ignore[no-untyped-def]
    assert await _publish_market(storage, settings) == 1
    async with get_sessionmaker()() as s:
        market = MarketService(s, storage, settings)
        first = await market.install("browser", None, actor="admin")
        again = await market.install("browser", 1, actor="admin")
        await s.commit()
    assert (first.version, first.copied, again.copied) == (1, True, False)
    assert storage.list_keys(f"{TEST_TENANT}/_skills/browser/1/") == [
        f"{TEST_TENANT}/_skills/browser/1/SKILL.md"
    ]

    async with get_sessionmaker()() as s:
        svc = SkillService(s, storage, settings)
        [item] = await svc.catalog()
        assert (item.slug, item.source, item.latest) == ("browser", "market", 1)
        # 来源是 market 的不能在租户里传新版本：新版本从市场装
        with pytest.raises(Conflict, match="技能市场"):
            await svc.ingest(_pkg(name="browser", body="改一下\n"), source="tenant", actor="a")
        listing = await MarketService(s, storage, settings).for_tenant()
        assert [(m.slug, m.installed) for m in listing] == [("browser", [1])]

    # 别的租户没装
    with TenantScope.enter(UUID(OTHER_TENANT)):
        async with get_sessionmaker()() as s:
            assert await SkillService(s, storage, settings).catalog() == []


async def test_install_refuses_own_skill_with_same_slug_and_revoked_versions(
    storage, settings
) -> None:  # type: ignore[no-untyped-def]
    await _publish_market(storage, settings)
    await _release(TEST_TENANT, _pkg(name="browser"), storage, settings)
    async with get_sessionmaker()() as s:
        with pytest.raises(Conflict, match="同名"):
            await MarketService(s, storage, settings).install("browser", None, actor="admin")

    async with get_sessionmaker()() as s:
        await MarketService(s, storage, settings).revoke("browser", 1, reason="有漏洞")
        await s.commit()
    with TenantScope.enter(UUID(OTHER_TENANT)):
        async with get_sessionmaker()() as s:
            with pytest.raises(Conflict, match="下架"):
                await MarketService(s, storage, settings).install("browser", 1, actor="admin")


async def test_tenant_admin_installs_from_market(
    client: httpx.AsyncClient, storage, settings
) -> None:  # type: ignore[no-untyped-def]
    await _publish_market(storage, settings)
    listing = (await client.get("/api/v1/market/skills")).json()
    assert [(m["slug"], m["latest"], m["installed"]) for m in listing] == [("browser", 1, [])]
    resp = await client.post("/api/v1/market/skills/browser/install")
    assert resp.json() == {"slug": "browser", "version": 1, "copied": True}
    catalog = (await client.get("/internal/skills/catalog")).json()
    assert [(c["slug"], c["source"]) for c in catalog] == [("browser", "market")]


async def test_ops_publishes_to_market_and_installs_defaults(client: httpx.AsyncClient) -> None:
    zipped = make_zip(skill_files(name="browser"))
    files = {"file": ("browser.zip", zipped, "application/zip")}
    resp = await _ops(
        client, "POST", "/ops/v1/market/skills", files=files, data={"install_by_default": "true"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["version"] == 1

    # 没签名的不是运营中心
    unsigned = await client.post("/ops/v1/market/skills", files=files)
    assert unsigned.status_code == 401

    # 开通租户（创建）：默认装全部「默认安装」的；能重做，装过的不再装一遍
    path = f"/ops/v1/tenants/{OTHER_TENANT}"
    for _ in range(2):
        created = await _ops(client, "PUT", path, json={})
        assert created.status_code == 200, created.text
    other = {"X-Tenant-Id": OTHER_TENANT}
    catalog = (await client.get("/internal/skills/catalog", headers=other)).json()
    assert [(c["slug"], c["latest"]) for c in catalog] == [("browser", 1)]
    assert (await client.get("/internal/skills/catalog")).json() == []
    # 开通时说不装：什么都不装；参数不对 → 409（要人看）
    third = "00000000-0000-0000-0000-00000000000c"
    none = await _ops(client, "PUT", f"/ops/v1/tenants/{third}", json={"install_defaults": False})
    assert none.status_code == 200
    catalog = (await client.get("/internal/skills/catalog", headers={"X-Tenant-Id": third})).json()
    assert catalog == []
    bad = await _ops(client, "PUT", f"/ops/v1/tenants/{third}", json={"skills": "browser"})
    assert bad.status_code == 409


# ───────────────────────────────────────────── 停用 / 删除租户（停用只在用户中心）


async def test_blocked_tenant_gets_no_permission(client: httpx.AsyncClient) -> None:
    """不知道租户状态、只认权限：租户停用后用户中心说这个人没有权限 → 403。"""
    BLOCKED_TENANTS.add(TEST_TENANT)
    r = await client.get("/api/v1/skills")
    assert r.status_code == 403
    # 别的租户不受影响
    other = {"X-Tenant-Id": OTHER_TENANT}
    assert (await client.get("/api/v1/skills", headers=other)).status_code == 200
    # 停用、恢复的运营接口没有了
    base = f"/ops/v1/tenants/{TEST_TENANT}"
    assert (await _ops(client, "POST", f"{base}/suspend")).status_code in (404, 405)


async def test_purge_deletes_only_this_tenants_rows_and_objects(
    client: httpx.AsyncClient, storage, settings
) -> None:  # type: ignore[no-untyped-def]
    await _release(TEST_TENANT, _pkg(), storage, settings)
    await _release(OTHER_TENANT, _pkg(), storage, settings)
    # 多租户之前发布、登记到 TEST_TENANT 的包（在桶根）
    storage.put_tree(legacy_prefix("legacy", 1), skill_files(name="legacy"))
    async with get_sessionmaker()() as s:
        await SkillService(s, storage, settings).adopt("legacy", 1, actor="ops")
        await s.commit()

    storage.put_tree(f"{TEST_TENANT}/workspace/", {"a.txt": "Atlas 的文件".encode()})

    resp = await _ops(client, "DELETE", f"/ops/v1/tenants/{TEST_TENANT}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["remaining"] is False

    assert storage.list_keys(f"{TEST_TENANT}/_skills") == []
    # 只删自己的子目录：同一个桶里别的服务的文件不动
    assert storage.list_keys(f"{TEST_TENANT}/workspace/") != []
    assert storage.list_keys("_skills/legacy/") == []
    assert storage.list_keys(f"{OTHER_TENANT}/_skills/demo/1/") != []
    async with get_sessionmaker()() as s:
        assert await SkillService(s, storage, settings).catalog() == []
    with TenantScope.enter(UUID(OTHER_TENANT)):
        async with get_sessionmaker()() as s:
            catalog = await SkillService(s, storage, settings).catalog()
            assert [c.slug for c in catalog] == ["demo"]
