"""容量（c4-model/agent/code-capacity.html、dynamic-tenant-quota.html）：租户份额和每人上限的准入。

run     份额满了，多出来的留在排队里（不算失败），有空了再抢得到；接管孤儿 run 不会被
        它自己挡住；每人上限按发起人数；别的租户的 run 不算
沙箱    要登记新沙箱时判租户份额，几个人并发新建也不超
存储    租户的用量 = 各人最近一次现算的用量之和；份额不限时不现算
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from atlas_agent.capacity import CapacityService, Quota, ResourceKind
from atlas_agent.config import get_settings
from atlas_agent.db.models import Run, StorageUsageRow
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.executor.continuation import Continuation
from atlas_agent.executor.scheduling import RunWorker
from atlas_agent.repositories.run_queue import RunQueue
from atlas_agent.runtime_config import RuntimeConfig
from atlas_tenancy import TenantScope
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.fake_uc import TEST_TENANT

pytestmark = pytest.mark.usefixtures("clean_db")

OTHER_TENANT = UUID("00000000-0000-0000-0000-00000000000b")


class _ParkedSegment:
    """一段一直跑到测试放行：只为让 run 持有租约。"""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def run(self, run_id: UUID, slot: Any, takeover: str | None) -> None:
        await self.release.wait()


@pytest.fixture
def capacity() -> CapacityService:
    return CapacityService(RuntimeConfig(get_settings(), get_sessionmaker()))


@pytest.fixture
async def worker(capacity: CapacityService) -> AsyncIterator[RunWorker]:
    sm, settings = get_sessionmaker(), get_settings()
    segment = _ParkedSegment()
    w = RunWorker(
        sm,
        settings,
        instance="cap-a",
        segment=segment,  # type: ignore[arg-type]
        continuation=Continuation(sm, settings, instance="cap-a"),
        capacity=capacity,
    )
    yield w
    segment.release.set()
    await asyncio.sleep(0)
    for slot in list(w._slots.values()):
        if slot.task is not None:
            slot.task.cancel()


async def _set_quota(capacity: CapacityService, quota: Quota | None) -> None:
    async with get_sessionmaker()() as s:
        await capacity.set_quota(s, quota, by="test")
        await s.commit()


async def _queued(user: UUID | None = None) -> UUID:
    async with get_sessionmaker()() as s:
        run = Run(
            run_uuid=uuid4(),
            thread_uuid=uuid4(),
            agent_version_uuid=uuid4(),
            status="queued",
            created_by_uuid=user or uuid4(),
        )
        s.add(run)
        await s.commit()
        return run.run_uuid


async def _run(run_id: UUID) -> Run:
    async with get_sessionmaker()() as s:
        return (await s.execute(select(Run).where(Run.run_uuid == run_id))).scalar_one()


async def _finish(run_id: UUID) -> None:
    async with get_sessionmaker()() as s:
        await s.execute(
            update(Run).where(Run.run_uuid == run_id).values(status="succeeded", owner=None)
        )
        await s.commit()


async def _take(capacity: CapacityService, run_id: UUID, owner: str) -> Any:
    """仓库层抢锁 + 准入，同一个事务（与 RunWorker.try_take 一样：配置在开事务之前读）。"""
    limits = await capacity.limits()
    async with get_sessionmaker()() as s:
        taken = await RunQueue(s).take(run_id, owner=owner, lease_s=30.0)
        assert taken is not None
        admission = await capacity.admit_run(s, run_id, limits)
        if admission.allowed:
            await s.commit()
        else:
            await s.rollback()
        return admission


# --------------------------------------------------------------------------- run


async def test_runs_over_the_quota_stay_queued_until_one_finishes(
    capacity: CapacityService, worker: RunWorker
) -> None:
    await _set_quota(capacity, Quota(running_runs=1))
    first, second = await _queued(), await _queued()

    assert await worker.try_take(first)
    free = worker.admission.free
    assert not await worker.try_take(second), "份额满了：不开跑"
    assert worker.admission.free == free, "没开跑就把槽还回去"
    held = await _run(second)
    assert (held.status, held.owner, held.claimed_at) == ("queued", None, None), "回滚抢锁"

    await _finish(first)
    assert await worker.try_take(second), "有空了，兜底下一轮再来就抢得到"


async def test_more_runs_starting_at_once_than_db_connections_do_not_deadlock() -> None:
    """准入不在认领事务里另借连接（capacity.py 模块说明）：池里只有 2 个连接、同时开跑 8 个，
    都要很快认领成功，不能「拿着一个、等第二个」卡到超时。

    2026-11-26 日本服务器压测：24 个同时开跑就卡死 30 秒。
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_size=2, max_overflow=0, pool_timeout=5)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    segment = _ParkedSegment()
    w = RunWorker(
        sm,
        settings,
        instance="cap-small",
        segment=segment,  # type: ignore[arg-type]
        continuation=Continuation(sm, settings, instance="cap-small"),
        capacity=CapacityService(RuntimeConfig(settings, sm)),
    )
    runs = [await _queued() for _ in range(8)]
    try:
        taken = await asyncio.wait_for(asyncio.gather(*(w.try_take(r) for r in runs)), 20)
        assert all(taken), taken
    finally:
        segment.release.set()
        await asyncio.sleep(0)
        for slot in list(w._slots.values()):
            if slot.task is not None:
                slot.task.cancel()
        await engine.dispose()


async def test_taking_over_an_orphan_is_not_blocked_by_itself(capacity: CapacityService) -> None:
    """份额 1，唯一的 run 所在实例崩了：租约过期后别的实例要能接管它。"""
    await _set_quota(capacity, Quota(running_runs=1))
    run_id = await _queued()
    assert (await _take(capacity, run_id, "dead")).allowed
    async with get_sessionmaker()() as s:
        past = datetime.now(UTC) - timedelta(seconds=1)
        await s.execute(update(Run).where(Run.run_uuid == run_id).values(lease_until=past))
        await s.commit()
    assert (await _take(capacity, run_id, "alive")).allowed


async def test_per_person_limit_counts_runs_by_who_started_them(capacity: CapacityService) -> None:
    config = RuntimeConfig(get_settings(), get_sessionmaker()).config
    await config.register()  # 宿主启动时做的登记
    tenant = UUID(TEST_TENANT)
    entry = await config.entry("capacity.runs_per_person", tenant_uuid=tenant)
    version = entry.item.version
    await config.set(
        "capacity.runs_per_person", 1, tenant_uuid=tenant, expected_version=version, by="t"
    )
    try:
        alice, bob = uuid4(), uuid4()
        assert (await _take(capacity, await _queued(alice), "a")).allowed
        refused = await _take(capacity, await _queued(alice), "a")
        assert not refused.allowed and refused.layer == "person" and refused.used == 1
        assert "你的" in refused.message
        assert (await _take(capacity, await _queued(bob), "a")).allowed, "别人的不算"
    finally:
        entry = await config.entry("capacity.runs_per_person", tenant_uuid=tenant)
        await config.set(
            "capacity.runs_per_person",
            None,
            tenant_uuid=tenant,
            expected_version=entry.item.version,
            by="t",
        )


async def test_other_tenants_runs_do_not_count(capacity: CapacityService) -> None:
    with TenantScope.enter(OTHER_TENANT):
        await _set_quota(capacity, Quota(running_runs=1))
        assert (await _take(capacity, await _queued(), "a")).allowed
    await _set_quota(capacity, Quota(running_runs=1))
    assert (await _take(capacity, await _queued(), "a")).allowed, "另一个租户在跑的不占本租户的份额"
    refused = await _take(capacity, await _queued(), "a")
    assert not refused.allowed and refused.layer == "tenant" and refused.kind is ResourceKind.RUN


async def test_quota_defaults_and_usage(capacity: CapacityService) -> None:
    async with get_sessionmaker()() as s:
        quota, is_default = await capacity.quota(s)
    assert is_default and quota == Quota(), "系统默认份额是 0 = 不限"

    await _set_quota(capacity, Quota(running_runs=3, sandboxes=2, storage_bytes=1024))
    assert (await _take(capacity, await _queued(), "a")).allowed
    async with get_sessionmaker()() as s:
        usage = await capacity.usage(s)
    assert not usage.is_default and usage.quota.running_runs == 3
    assert (usage.running_runs, usage.sandboxes, usage.storage_bytes) == (1, 0, 0)

    await _set_quota(capacity, None)
    async with get_sessionmaker()() as s:
        assert await capacity.quota(s) == (Quota(), True), "回到默认份额"


# --------------------------------------------------------------------------- 存储


async def test_storage_sums_everyones_last_measured_usage(capacity: CapacityService) -> None:
    measured: list[int] = []

    async def measure() -> int:
        measured.append(30)
        return 30

    limits = await capacity.limits()
    async with get_sessionmaker()() as s:
        allowed = await capacity.admit_storage(
            s, uuid4(), measure=measure, adding=10**9, limits=limits
        )
    assert allowed.allowed and measured == [], "份额不限：不现算"

    await _set_quota(capacity, Quota(storage_bytes=100))
    other, me = uuid4(), uuid4()
    async with get_sessionmaker()() as s:
        await capacity.record_storage(s, other, 60)
        await s.commit()
    limits = await capacity.limits()
    async with get_sessionmaker()() as s:
        refused = await capacity.admit_storage(s, me, measure=measure, adding=20, limits=limits)
        assert not refused.allowed and refused.used == 90 and refused.limit == 100
        ok = await capacity.admit_storage(s, me, measure=measure, adding=10, limits=limits)
        assert ok.allowed
        await s.commit()
    async with get_sessionmaker()() as s:
        mine = await s.scalar(select(StorageUsageRow.bytes).where(StorageUsageRow.user_uuid == me))
    assert mine == 30, "现算的用量顺手记下"
