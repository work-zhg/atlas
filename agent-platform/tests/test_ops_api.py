"""Atlas 的运营接口（c4-model/agent/code-ops.html；dynamic-tenant-open、-close、-quota）。

    签名    没签名、签错了的一律 401；没配运营密钥 = 没启用，403
    创建    开通时填了份额就记下，没填用默认；能重做；参数不对 409
    停用    不在这里：Atlas 不知道租户状态。用户中心对停用租户的人回答无权限 → 403；
            在跑的 run 照常（设计接受的代价）
    删除    分批删到没有剩，别的租户的数据不动；不查租户状态（只删已停用的由运营中心保证）
    份额    改一个租户的份额 / 回到默认份额 / 改默认份额；用量只有数字

★ 每个用例用一个新的租户 uuid。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from atlas_agent.config import get_settings
from atlas_agent.db.models import AppUser, Run, TenantCapacityRow
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.main import create_app
from atlas_appconfig.db import config_table, revision_table
from atlas_tenancy import TenantScope
from fastapi import FastAPI
from httpx import ASGITransport

from tests.fake_uc import FakeUC
from tests.ops_env import ops_call

pytestmark = pytest.mark.usefixtures("clean_db")


@pytest.fixture
def tenant() -> UUID:
    return uuid4()


@pytest.fixture
async def app(ops_key: str) -> AsyncIterator[FastAPI]:
    yield create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _tenant_path(tenant: UUID) -> str:
    return f"/ops/v1/tenants/{tenant}"


async def _queued_run(tenant: UUID) -> UUID:
    with TenantScope.enter(tenant):
        async with get_sessionmaker()() as s:
            run = Run(
                run_uuid=uuid4(),
                thread_uuid=uuid4(),
                agent_version_uuid=uuid4(),
                status="queued",
                created_by_uuid=uuid4(),
            )
            s.add(run)
            await s.commit()
            return run.run_uuid


# --------------------------------------------------------------------------- 签名


async def test_only_signed_requests_get_in(client: httpx.AsyncClient, tenant: UUID) -> None:
    path = _tenant_path(tenant)
    assert (await client.put(path, json={})).status_code == 401
    assert (await ops_call(client, "PUT", path, {}, key="wrong-key")).status_code == 401
    assert (await ops_call(client, "PUT", path, {})).status_code == 200


async def test_without_a_key_the_ops_api_is_off(tenant: UUID) -> None:
    app = create_app()  # 没有 ops_key 夹具：没配运营密钥
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await ops_call(c, "PUT", _tenant_path(tenant), {})
    assert r.status_code == 403


# --------------------------------------------------------------------------- 创建


@pytest.mark.usefixtures("defaults_restored")
async def test_create_records_the_quota_given_at_opening(
    client: httpx.AsyncClient, tenant: UUID
) -> None:
    usage = f"/ops/v1/usage/{tenant}"
    # 没填份额：不建行，用默认份额
    assert (await ops_call(client, "PUT", _tenant_path(tenant), {})).status_code == 200
    assert (await ops_call(client, "GET", usage)).json()["is_default"] is True
    # 填了：记下；超时后重做，结果一样
    body = {"running_runs": 3, "sandboxes": 2, "storage_bytes": None}
    for _ in range(2):
        r = await ops_call(client, "PUT", _tenant_path(tenant), body)
        assert r.status_code == 200, r.text
    got = (await ops_call(client, "GET", usage)).json()
    assert got["is_default"] is False
    assert got["quota"] == {"running_runs": 3, "sandboxes": 2, "storage_bytes": None}
    # 参数不对：被拒绝（要人看）
    r = await ops_call(client, "PUT", _tenant_path(tenant), {"sandboxes": -1})
    assert r.status_code == 409


# --------------------------------------------------------------------------- 停用（只在用户中心）


async def test_a_blocked_tenants_people_get_no_permission(
    client: httpx.AsyncClient, fake_user_center: FakeUC, tenant: UUID
) -> None:
    """用户中心对停用、到期租户的人回答无权限（can_access 为假）→ 403。
    停用、恢复的运营接口没有了。"""
    blocked = {"X-Uid": fake_user_center.add("member", [], can_access=False, tenant=str(tenant))}
    assert (await client.get("/v1/agents", headers=blocked)).status_code == 403
    for action in ("suspend", "resume"):
        r = await ops_call(client, "POST", f"{_tenant_path(tenant)}/{action}")
        assert r.status_code in (404, 405)


# --------------------------------------------------------------------------- 删除


async def test_delete_removes_only_this_tenants_data_in_batches(
    client: httpx.AsyncClient, tenant: UUID
) -> None:
    with TenantScope.enter(tenant):
        async with get_sessionmaker()() as s:
            s.add(AppUser(user_uuid=uuid4(), name="将被删除"))
            s.add(TenantCapacityRow(is_default=True))
            await s.commit()
    await _queued_run(tenant)
    neighbour = uuid4()
    survivor = await _queued_run(neighbour)

    deleted: dict[str, int] = {}
    for _ in range(10):  # 运营中心一批批调，直到没有剩
        r = await ops_call(client, "DELETE", _tenant_path(tenant))
        assert r.status_code == 200, r.text
        for table, n in r.json()["deleted"].items():
            deleted[table] = deleted.get(table, 0) + n
        if not r.json()["remaining"]:
            break
    assert {"run": 1, "app_user": 1, "tenant_capacity": 1}.items() <= deleted.items()

    async with get_sessionmaker()() as s:
        with TenantScope.enter(tenant):
            assert await s.scalar(sa.select(sa.func.count(Run.id))) == 0
        with TenantScope.enter(neighbour):
            left = await s.scalar(sa.select(Run.run_uuid))
    assert left == survivor, "别的租户的数据不动"
    # 删完再调：照样回 remaining=false（重做安全）
    again = await ops_call(client, "DELETE", _tenant_path(tenant))
    assert again.status_code == 200 and again.json()["remaining"] is False


# --------------------------------------------------------------------------- 份额


@pytest.fixture
async def defaults_restored(app: FastAPI) -> AsyncIterator[None]:
    await app.state.runtime_config.config.register()
    yield
    names = [
        "capacity.default_running_runs",
        "capacity.default_sandboxes",
        "capacity.default_storage_mb",
    ]
    async with get_sessionmaker()() as s:
        ids = sa.select(config_table.c.config_uuid).where(config_table.c.key.in_(names))
        await s.execute(sa.delete(revision_table).where(revision_table.c.config_uuid.in_(ids)))
        await s.execute(sa.delete(config_table).where(config_table.c.key.in_(names)))
        await s.commit()


@pytest.mark.usefixtures("defaults_restored")
async def test_quotas_per_tenant_and_the_default(client: httpx.AsyncClient, tenant: UUID) -> None:
    r = await ops_call(client, "PUT", f"/ops/v1/quotas/{tenant}", {"running_runs": 2})
    assert r.status_code == 200, r.text
    assert r.json()["quota"] == {"running_runs": 2, "sandboxes": None, "storage_bytes": None}
    assert r.json()["is_default"] is False

    listed = (await ops_call(client, "GET", "/ops/v1/quotas")).json()
    assert listed["default"] == {"running_runs": None, "sandboxes": None, "storage_bytes": None}
    mine = next(t for t in listed["tenants"] if t["tenant_uuid"] == str(tenant))
    assert mine["running_runs"] == 2 and mine["updated_by"] == "运营：值班"

    mb = 1024 * 1024
    r = await ops_call(
        client, "PUT", "/ops/v1/quotas/default", {"sandboxes": 5, "storage_bytes": 3 * mb + 1}
    )
    assert r.json() == {"running_runs": None, "sandboxes": 5, "storage_bytes": 3 * mb}

    r = await ops_call(client, "PUT", f"/ops/v1/quotas/{tenant}", {"use_default": True})
    assert r.json()["is_default"] is True
    assert r.json()["quota"] == {"running_runs": None, "sandboxes": 5, "storage_bytes": 3 * mb}
    listed = (await ops_call(client, "GET", "/ops/v1/quotas")).json()
    assert str(tenant) not in {t["tenant_uuid"] for t in listed["tenants"]}

    await _queued_run(tenant)
    usage = (await ops_call(client, "GET", f"/ops/v1/usage/{tenant}")).json()
    assert usage["usage"] == {"running_runs": 0, "sandboxes": 0, "storage_bytes": 0}
    total = (await ops_call(client, "GET", "/ops/v1/capacity")).json()
    assert total["runs_per_instance"] == get_settings().max_concurrent_runs
