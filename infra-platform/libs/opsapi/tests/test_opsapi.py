"""运营接口库的行为测试（c4-model/infra-platform/code-opsapi.html）。

签名和 OperatorStamp 不连库；挂起、恢复、删除的整条路连真库：只在库名以 _test 结尾时跑。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from atlas_appconfig import AppConfig, ConfigKey
from atlas_appconfig import upgrade as appconfig_upgrade
from atlas_appconfig.db import config_table, revision_table
from atlas_jobs import JobScheduler
from atlas_opsapi import (
    OperatorStamp,
    OpsGuard,
    OpsRoutes,
    SignatureError,
    TenantGate,
    TenantLifecycleService,
    sign,
    upgrade,
    verify,
)
from atlas_opsapi.db import request_table, state_table
from atlas_tenancy import PurgeReport, TenantDataPurge, TenantOwned, TenantScope
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

KEY = b"ops-secret-for-tests"

# ──────────────────────────────────────────────── 签名、OperatorStamp（不连库）


def test_signature_round_trip_and_tampering() -> None:
    headers = sign(KEY, "post", "/ops/v1/tenants/x/suspend?a=1", b'{"a":1}', at=1000)
    verify(KEY, "POST", "/ops/v1/tenants/x/suspend?a=1", b'{"a":1}', headers, now=1100)
    with pytest.raises(SignatureError, match="签名不对"):
        verify(KEY, "POST", "/ops/v1/tenants/x/suspend?a=1", b'{"a":2}', headers, now=1100)
    with pytest.raises(SignatureError, match="签名不对"):
        verify(KEY, "POST", "/ops/v1/tenants/y/suspend?a=1", b'{"a":1}', headers, now=1100)
    with pytest.raises(SignatureError, match="签名不对"):
        verify(
            b"another-key", "POST", "/ops/v1/tenants/x/suspend?a=1", b'{"a":1}', headers, now=1100
        )
    with pytest.raises(SignatureError, match="过期"):
        verify(KEY, "POST", "/ops/v1/tenants/x/suspend?a=1", b'{"a":1}', headers, now=1000 + 301)
    with pytest.raises(SignatureError, match="缺少"):
        verify(KEY, "POST", "/x", b"", {}, now=1000)


def test_operator_headers_are_signed_too() -> None:
    """★ 操作人和幂等键进签名原文：截到一个请求，不能在窗口内改掉操作人，也不能换个幂等键重放
    （业务系统按幂等键去重，不签它的话去重形同虚设）。"""
    path, body = "/ops/v1/tenants/x/suspend", b"{}"
    stamp = OperatorStamp(uuid4(), "小周", uuid4(), uuid4())
    headers = sign(KEY, "POST", path, body, stamp=stamp, at=1000)
    assert OperatorStamp.from_headers(headers) == stamp  # 运营头随签名一起给出
    verify(KEY, "POST", path, body, headers, now=1000)

    replayed = {**headers, "Idempotency-Key": str(uuid4())}
    with pytest.raises(SignatureError, match="签名不对"):
        verify(KEY, "POST", path, body, replayed, now=1000)
    forged = {**headers, "X-Ops-Operator": str(uuid4())}
    with pytest.raises(SignatureError, match="签名不对"):
        verify(KEY, "POST", path, body, forged, now=1000)
    added = {**sign(KEY, "POST", path, body, at=1000), "X-Ops-Operator-Name": "%E5%86%92%E5%85%85"}
    with pytest.raises(SignatureError, match="签名不对"):
        verify(KEY, "POST", path, body, added, now=1000)


def test_operator_stamp_survives_headers_with_chinese_name() -> None:
    stamp = OperatorStamp(uuid4(), "小周", uuid4(), uuid4())
    headers = stamp.to_headers()
    assert all(v.isascii() for v in headers.values())
    assert OperatorStamp.from_headers(headers) == stamp
    assert stamp.who == "运营：小周"
    assert OperatorStamp().who == "运营中心"


# ──────────────────────────────────────────────── 整条路（真库）

DB = os.environ.get("DATABASE_URL", "")
pytestmark_db = pytest.mark.skipif(
    not DB.endswith("_test"), reason="只在测试库上跑（DATABASE_URL 以 _test 结尾）"
)

SUFFIX = uuid4().hex[:8]


class Base(DeclarativeBase):
    pass


class Doc(TenantOwned, Base):
    __tablename__ = f"opsapi_doc_{SUFFIX}"
    id: Mapped[int] = mapped_column(primary_key=True)


class Hooks:
    """宿主的钩子：记下调用时所在的租户；外部资源分两批删完。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, UUID | None]] = []
        self.external_left = 2

    async def on_suspend(self, tenant_uuid: UUID) -> None:
        self.calls.append(("suspend", TenantScope.current()))

    async def on_resume(self, tenant_uuid: UUID) -> None:
        self.calls.append(("resume", TenantScope.current()))

    async def purge_external(self, tenant_uuid: UUID, limit: int) -> PurgeReport:
        self.calls.append(("purge_external", TenantScope.current()))
        self.external_left -= 1
        return PurgeReport({"objects": 10}, remaining=self.external_left > 0)


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(DB)
    async with engine.begin() as conn:
        await conn.run_sync(upgrade)
        await conn.run_sync(appconfig_upgrade)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


class Signed:
    """替运营中心发请求：带签名和 OperatorStamp。"""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def call(
        self, method: str, path: str, body: object | None = None, *, key: UUID | None = None
    ) -> httpx.Response:
        raw = b"" if body is None else json.dumps(body).encode()
        stamp = OperatorStamp(uuid4(), "小周", uuid4(), key)
        headers = sign(KEY, method, path, raw, stamp=stamp)
        if raw:
            headers["Content-Type"] = "application/json"
        return await self.client.request(method, path, content=raw, headers=headers)


@pytestmark_db
async def test_suspend_purge_and_resume_rules(sessions: async_sessionmaker[AsyncSession]) -> None:
    tenant, other = uuid4(), uuid4()
    async with sessions() as s:
        s.add_all([Doc(id=i, tenant_uuid=tenant if i <= 3 else other) for i in range(1, 6)])
        await s.commit()
    hooks = Hooks()
    gate = TenantGate(sessions)
    lifecycle = TenantLifecycleService(sessions, hooks, gate, TenantDataPurge([Doc]), purge_limit=2)
    app = FastAPI()
    app.include_router(OpsRoutes(OpsGuard(KEY), lifecycle).router)
    base = f"/ops/v1/tenants/{tenant}"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            ops = Signed(c)
            # 不是运营中心：没签名、签错
            assert (await c.post(f"{base}/suspend")).status_code == 401
            assert (
                await c.post(f"{base}/suspend", headers=sign(b"x", "POST", f"{base}/suspend"))
            ).status_code == 401
            # 还在用的租户不能删
            assert (await ops.call("POST", f"{base}/purge", key=uuid4())).status_code == 409

            step = uuid4()
            r = await ops.call("POST", f"{base}/suspend", key=step)
            assert r.json() == {"status": "suspended"}
            assert not await gate.is_active(tenant) and await gate.is_active(other)
            # 同一个幂等键重做：直接返回上次的结果，不再调钩子
            assert (await ops.call("POST", f"{base}/suspend", key=step)).json() == {
                "status": "suspended"
            }
            assert hooks.calls == [("suspend", tenant)]

            # 删除分批：外部两批，数据库每批 2 行，同一个幂等键一直调到删完。
            # 第 1 次：外部还有剩；第 2 次：外部删完，接着删数据库 2 行（还剩 1 行）；第 3 次：删完
            close = uuid4()
            results = []
            for _ in range(5):
                body = (await ops.call("POST", f"{base}/purge", key=close)).json()
                results.append(body)
                if not body["remaining"]:
                    break
            assert [r["remaining"] for r in results] == [True, True, False]
            assert sum(r["deleted"].get(Doc.__tablename__, 0) for r in results) == 3
            async with sessions() as s:
                from atlas_tenancy import SystemReason

                with TenantScope.system(SystemReason.OPS):
                    left = (await s.execute(sa.select(Doc.tenant_uuid))).scalars().all()
            assert left == [other, other]
            # 删完以后同一个幂等键：返回记下的结果；不能再恢复
            assert (await ops.call("POST", f"{base}/purge", key=close)).json() == results[-1]
            assert (await ops.call("POST", f"{base}/resume", key=uuid4())).status_code == 409

            states = (await ops.call("GET", "/ops/v1/tenant-states")).json()
            assert next(s for s in states if s["tenant_uuid"] == str(tenant))["status"] == "purged"
    finally:
        async with sessions() as s:
            await s.execute(
                sa.delete(state_table).where(state_table.c.tenant_uuid.in_([tenant, other]))
            )
            await s.execute(
                sa.delete(request_table).where(request_table.c.tenant_uuid.in_([tenant, other]))
            )
            await s.commit()


@pytestmark_db
async def test_resume_runs_hook_in_tenant_then_reactivates(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    tenant = uuid4()
    hooks = Hooks()
    gate = TenantGate(sessions)
    lifecycle = TenantLifecycleService(sessions, hooks, gate)
    app = FastAPI()
    app.include_router(OpsRoutes(OpsGuard(KEY), lifecycle).router)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            ops = Signed(c)
            await ops.call("POST", f"/ops/v1/tenants/{tenant}/suspend", key=uuid4())
            r = await ops.call("POST", f"/ops/v1/tenants/{tenant}/resume", key=uuid4())
            assert r.json() == {"status": "active"}
            assert hooks.calls == [("suspend", tenant), ("resume", tenant)]
            assert await gate.is_active(tenant)
    finally:
        async with sessions() as s:
            await s.execute(sa.delete(state_table).where(state_table.c.tenant_uuid == tenant))
            await s.execute(sa.delete(request_table).where(request_table.c.tenant_uuid == tenant))
            await s.commit()


@pytestmark_db
async def test_system_params_and_jobs_are_only_for_ops_center(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    name = f"t{SUFFIX}.images"
    config = AppConfig(sessions, [ConfigKey(name, list[str], default=["a/"])])
    await config.register()
    lifecycle = TenantLifecycleService(sessions, Hooks(), TenantGate(sessions))
    app = FastAPI()
    app.include_router(
        OpsRoutes(OpsGuard(KEY), lifecycle, config=config, jobs=JobScheduler()).router
    )
    disabled = FastAPI()
    disabled.include_router(OpsRoutes(OpsGuard(None), lifecycle, config=config).router)
    try:
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=disabled), base_url="http://t"
            ) as d,
        ):
            ops = Signed(c)
            assert (await c.get("/ops/v1/configs")).status_code == 401
            assert (await Signed(d).call("GET", "/ops/v1/configs")).status_code == 403
            listed = (await ops.call("GET", "/ops/v1/configs")).json()
            assert any(i["key"] == name for i in listed)
            r = await ops.call(
                "PUT", f"/ops/v1/configs/{name}", {"value": ["b/"], "expected_version": 1}
            )
            assert r.status_code == 200 and r.json()["updated_by"] == "运营：小周"
            assert (await ops.call("GET", "/ops/v1/jobs")).status_code == 200
    finally:
        async with sessions() as s:
            ids = sa.select(config_table.c.config_uuid).where(config_table.c.key == name)
            await s.execute(sa.delete(revision_table).where(revision_table.c.config_uuid.in_(ids)))
            await s.execute(sa.delete(config_table).where(config_table.c.key == name))
            await s.commit()
