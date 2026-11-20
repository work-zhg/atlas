"""Atlas 的运营接口（c4-model/agent/code-ops.html；dynamic-tenant-suspend、-close、-quota）。

    签名    没签名、签错了的一律 401；没配运营密钥 = 没启用，403
    停用    本地状态改成停用：这个租户的人的请求 403，它的 run 谁也不接（留在排队里），本实例上
            在跑的交出去；恢复后一切照常
    删除    只删已停用的租户；分批删到没有剩，别的租户的数据不动；删过的不能恢复
    份额    改一个租户的份额 / 回到默认份额 / 改默认份额；用量只有数字

★ 每个用例用一个新的租户 uuid，测完删掉它在运营接口库里的状态和请求记录（测试库共用）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from atlas_agent.config import get_settings
from atlas_agent.db.models import AppUser, Device, Run, TenantCapacityRow
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.executor.continuation import Continuation
from atlas_agent.executor.scheduling import ExecutionSlot, RunWorker
from atlas_agent.main import create_app
from atlas_appconfig.db import config_table, revision_table
from atlas_opsapi.db import request_table, state_table
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
async def app(ops_key: str, tenant: UUID) -> AsyncIterator[FastAPI]:
    app = create_app()
    yield app
    async with get_sessionmaker()() as s:
        await s.execute(sa.delete(state_table).where(state_table.c.tenant_uuid == tenant))
        await s.execute(sa.delete(request_table).where(request_table.c.tenant_uuid == tenant))
        await s.commit()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _tenant_path(tenant: UUID, action: str) -> str:
    return f"/ops/v1/tenants/{tenant}/{action}"


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
    path = _tenant_path(tenant, "suspend")
    assert (await client.post(path)).status_code == 401
    assert (await ops_call(client, "POST", path, key="wrong-key")).status_code == 401
    assert (await ops_call(client, "GET", "/ops/v1/tenant-states")).status_code == 200


async def test_without_a_key_the_ops_api_is_off(tenant: UUID) -> None:
    app = create_app()  # 没有 ops_key 夹具：没配运营密钥
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await ops_call(c, "POST", _tenant_path(tenant, "suspend"))
    assert r.status_code == 403


# --------------------------------------------------------------------------- 停用、恢复


async def test_suspend_shuts_the_tenant_out_and_resume_lets_it_back(
    app: FastAPI, client: httpx.AsyncClient, fake_user_center: FakeUC, tenant: UUID
) -> None:
    user = {"X-Uid": fake_user_center.add("member", ["chat:use"], tenant=str(tenant))}
    assert (await client.get("/v1/agents", headers=user)).status_code == 200

    key = uuid4()
    r = await ops_call(client, "POST", _tenant_path(tenant, "suspend"), idempotency_key=key)
    assert r.status_code == 200 and r.json() == {"status": "suspended"}
    again = await ops_call(client, "POST", _tenant_path(tenant, "suspend"), idempotency_key=key)
    assert again.json() == r.json(), "同一个幂等键：直接返回上次的结果"
    r = await client.get("/v1/agents", headers=user)
    assert r.status_code == 403 and "停用" in r.json()["error"]["message"]

    states = (await ops_call(client, "GET", "/ops/v1/tenant-states")).json()
    assert {"tenant_uuid": str(tenant), "status": "suspended"}.items() <= next(
        s for s in states if s["tenant_uuid"] == str(tenant)
    ).items()

    r = await ops_call(client, "POST", _tenant_path(tenant, "resume"))
    assert r.status_code == 200 and r.json() == {"status": "active"}
    assert (await client.get("/v1/agents", headers=user)).status_code == 200


async def test_suspend_disconnects_the_tenants_online_devices(
    client: httpx.AsyncClient, tenant: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停用时断开这个租户在线的本机设备；不在线的、吊销了的、别的租户的都不碰。"""
    online, asleep, revoked, other = uuid4(), uuid4(), uuid4(), uuid4()
    calls: list[tuple[str, str]] = []

    class _Gateway:
        def __init__(self, _settings: Any) -> None:
            self.configured = True

        async def presence(self, device: str) -> object | None:
            return object() if device in (str(online), str(revoked), str(other)) else None

        async def disconnect(self, device: str, reason: str) -> None:
            calls.append((device, reason))

    monkeypatch.setattr("atlas_agent.ops.lifecycle.DeviceGateway", _Gateway)
    for owner_tenant, device, status in (
        (tenant, online, "active"),
        (tenant, asleep, "active"),
        (tenant, revoked, "revoked"),
        (uuid4(), other, "active"),
    ):
        with TenantScope.enter(owner_tenant):
            async with get_sessionmaker()() as s:
                s.add(
                    Device(
                        device_uuid=device,
                        owner_uuid=uuid4(),
                        name="MacBook",
                        token_hash=uuid4().hex * 2,
                        status=status,
                    )
                )
                await s.commit()

    assert (await ops_call(client, "POST", _tenant_path(tenant, "suspend"))).status_code == 200
    assert calls == [(str(online), "企业账号已停用")]


class _ParkedSegment:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def run(self, run_id: UUID, slot: Any, takeover: str | None) -> None:
        await self.release.wait()


async def test_a_suspended_tenants_runs_wait_and_running_ones_are_handed_off(
    app: FastAPI, client: httpx.AsyncClient, tenant: UUID
) -> None:
    sm, settings = get_sessionmaker(), get_settings()
    segment = _ParkedSegment()
    worker = RunWorker(
        sm,
        settings,
        instance="ops-a",
        segment=segment,  # type: ignore[arg-type]
        continuation=Continuation(sm, settings, instance="ops-a"),
        gate=app.state.tenant_gate,
    )
    app.state.executor = worker  # 停用请求打到的就是这个实例
    try:
        running, waiting = await _queued_run(tenant), await _queued_run(tenant)
        with TenantScope.enter(tenant):
            assert await worker.try_take(running)
        slot: ExecutionSlot = worker.live_slots()[running]
        assert slot.tenant == tenant

        assert (await ops_call(client, "POST", _tenant_path(tenant, "suspend"))).status_code == 200
        assert slot.drain, "本实例上在跑的交出去（native 到安全点）"
        with TenantScope.enter(tenant):
            assert not await worker.try_take(waiting), "停用期间谁也不接"
            async with sm() as s:
                row = (await s.execute(sa.select(Run).where(Run.run_uuid == waiting))).scalar_one()
        assert (row.status, row.owner) == ("queued", None)

        assert (await ops_call(client, "POST", _tenant_path(tenant, "resume"))).status_code == 200
        with TenantScope.enter(tenant):
            assert await worker.try_take(waiting), "恢复后照常接"
    finally:
        segment.release.set()
        await asyncio.sleep(0)
        for s in list(worker._slots.values()):
            if s.task is not None:
                s.task.cancel()


async def test_other_instances_hand_off_on_their_next_renewal(tenant: UUID) -> None:
    """停用请求打到了别的实例：本实例续租时查到租户已停用，自己交出去。"""

    class _Gate:
        active = True

        async def is_active(self, t: UUID) -> bool:
            return self.active or t != tenant

    sm, settings = get_sessionmaker(), get_settings()
    gate = _Gate()
    worker = RunWorker(
        sm,
        settings,
        instance="ops-b",
        segment=_ParkedSegment(),  # type: ignore[arg-type]
        continuation=Continuation(sm, settings, instance="ops-b"),
        gate=gate,  # type: ignore[arg-type]
    )
    mine, other = ExecutionSlot(tenant=tenant), ExecutionSlot(tenant=uuid4())
    worker._slots.update({uuid4(): mine, uuid4(): other})
    await worker.pause_inactive_tenants()
    assert not mine.drain
    gate.active = False
    await worker.pause_inactive_tenants()
    assert mine.drain and not other.drain


# --------------------------------------------------------------------------- 删除


async def test_purge_only_after_suspend_and_only_this_tenants_data(
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

    r = await ops_call(client, "POST", _tenant_path(tenant, "purge"))
    assert r.status_code == 409 and "先停用" in r.text
    assert (await ops_call(client, "POST", _tenant_path(tenant, "suspend"))).status_code == 200

    key = uuid4()
    deleted: dict[str, int] = {}
    for _ in range(10):  # 运营中心用同一个幂等键一批批调，直到没有剩
        r = await ops_call(client, "POST", _tenant_path(tenant, "purge"), idempotency_key=key)
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
    r = await ops_call(client, "POST", _tenant_path(tenant, "resume"))
    assert r.status_code == 409, "删过的不能恢复"


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
