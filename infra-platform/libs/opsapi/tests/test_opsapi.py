"""运营接口库的行为测试（c4-model/infra-platform/code-opsapi.html）。

签名和 OperatorStamp 不连库；创建、删除的整条路连真库：只在库名以 _test 结尾时跑。
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
    Rejected,
    SignatureError,
    TenantLifecycle,
    sign,
    upgrade,
    verify,
)
from atlas_tenancy import PurgeReport, SystemReason, TenantOwned, TenantScope
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
    """★ 操作人和幂等键进签名原文：截到一个请求，不能在窗口内改掉操作人或幂等键
    （业务系统按它们写日志，和运营中心的审计对上）。"""
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


class Quota(TenantOwned, Base):
    __tablename__ = f"opsapi_quota_{SUFFIX}"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mb: Mapped[int] = mapped_column()


class Lifecycle(TenantLifecycle):
    """宿主：创建时记份额（再建一次覆盖，结果一样）；外部资源分两批删完。"""

    purge_order = (Doc, Quota)

    def __init__(self, sessions: async_sessionmaker[AsyncSession], **kw: object) -> None:
        super().__init__(sessions, base=Base, **kw)  # type: ignore[arg-type]
        self.calls: list[tuple[str, UUID | None]] = []
        self.external_left = 2

    async def create(self, tenant_uuid: UUID, params: dict[str, object]) -> None:
        self.calls.append(("create", TenantScope.current()))
        mb = params.get("storage_mb", 100)
        if not isinstance(mb, int) or mb < 0:
            raise Rejected("storage_mb 必须是非负整数")
        async with self._sessions() as s:
            row = (await s.execute(sa.select(Quota))).scalar_one_or_none()
            if row is None:
                s.add(Quota(mb=mb))
            else:
                row.mb = mb
            await s.commit()

    async def delete_external(self, tenant_uuid: UUID, budget: int) -> PurgeReport:
        self.calls.append(("delete_external", TenantScope.current()))
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


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _quotas(sessions: async_sessionmaker[AsyncSession]) -> list[tuple[UUID, int]]:
    async with sessions() as s:
        with TenantScope.system(SystemReason.OPS):
            rows = (await s.execute(sa.select(Quota.tenant_uuid, Quota.mb))).all()
    return [(r[0], r[1]) for r in rows]


def test_lifecycle_checks_purge_order_against_base() -> None:
    """漏了租户表构造时就报错：删租户时不会留下没删的表。"""

    class Missing(Lifecycle):
        purge_order = (Doc,)

    with pytest.raises(ValueError, match="漏了租户表"):
        Missing(async_sessionmaker())


@pytestmark_db
async def test_create_runs_in_tenant_and_can_be_redone(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    tenant = uuid4()
    lifecycle = Lifecycle(sessions)
    app = FastAPI()
    OpsRoutes.mount(app, OpsGuard(KEY), lifecycle)
    path = f"/ops/v1/tenants/{tenant}"
    async with _client(app) as c:
        ops = Signed(c)
        # 不是运营中心：没签名、签错
        assert (await c.put(path, json={})).status_code == 401
        assert (
            await c.put(path, content=b"{}", headers=sign(b"x", "PUT", path, b"{}"))
        ).status_code == 401

        r = await ops.call("PUT", path, {"storage_mb": 50}, key=uuid4())
        assert r.status_code == 200 and r.json() == {"tenant_uuid": str(tenant)}
        assert lifecycle.calls == [("create", tenant)]
        # 超时后重做（换了幂等键也一样）：结果一样，不会多出一行
        await ops.call("PUT", path, {"storage_mb": 50}, key=uuid4())
        assert (tenant, 50) in await _quotas(sessions)
        assert [q for q in await _quotas(sessions) if q[0] == tenant] == [(tenant, 50)]
        # 没有参数 = 用默认
        assert (await ops.call("PUT", f"/ops/v1/tenants/{uuid4()}")).status_code == 200
        # 参数不对：宿主拒绝 → 409（要人看）；不是 JSON 对象 → 422
        bad = await ops.call("PUT", path, {"storage_mb": -1})
        assert bad.status_code == 409 and "storage_mb" in bad.json()["detail"]
        assert (await ops.call("PUT", path, [1, 2])).status_code == 422
        # 停用、恢复都没有了
        assert (await ops.call("POST", f"{path}/suspend")).status_code in (404, 405)


@pytestmark_db
async def test_delete_external_first_then_rows_in_batches(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    tenant, other = uuid4(), uuid4()
    async with sessions() as s:
        s.add_all([Doc(id=i, tenant_uuid=tenant if i <= 3 else other) for i in range(1, 6)])
        await s.commit()
    lifecycle = Lifecycle(sessions, budget=2)
    app = FastAPI()
    OpsRoutes.mount(app, OpsGuard(KEY), lifecycle)
    path = f"/ops/v1/tenants/{tenant}"
    async with _client(app) as c:
        ops = Signed(c)
        assert (await c.delete(path)).status_code == 401
        # 外部两批，数据库每批 2 行：第 1 次外部还有剩；第 2 次外部删完，接着删数据库 2 行
        # （还剩 1 行）；第 3 次外部没东西了，删掉最后 1 行。不查租户状态，也不按幂等键去重
        results = []
        for _ in range(5):
            body = (await ops.call("DELETE", path, key=uuid4())).json()
            results.append(body)
            if not body["remaining"]:
                break
        assert [r["remaining"] for r in results] == [True, True, False]
        assert sum(r["deleted"].get(Doc.__tablename__, 0) for r in results) == 3
        assert results[0]["deleted"] == {"objects": 10}
        # 外部在这个租户的上下文里删
        assert lifecycle.calls[0] == ("delete_external", tenant)
        async with sessions() as s:
            with TenantScope.system(SystemReason.OPS):
                left = (await s.execute(sa.select(Doc.tenant_uuid))).scalars().all()
        assert left == [other, other]
        # 删完以后再调：什么都没有了，照样回 remaining=false（重做安全）
        again = (await ops.call("DELETE", path, key=uuid4())).json()
        assert again["remaining"] is False


@pytestmark_db
async def test_system_params_and_jobs_are_only_for_ops_center(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    name = f"t{SUFFIX}.images"
    config = AppConfig(sessions, [ConfigKey(name, list[str], default=["a/"])])
    await config.register()
    lifecycle = Lifecycle(sessions)
    app = FastAPI()
    OpsRoutes.mount(app, OpsGuard(KEY), lifecycle, config=config, jobs=JobScheduler())
    disabled = FastAPI()
    OpsRoutes.mount(disabled, OpsGuard(None), lifecycle, config=config)
    try:
        async with _client(app) as c, _client(disabled) as d:
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


@pytestmark_db
async def test_upgrade_drops_the_old_state_tables(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """存量库：0002 删掉 0001 建的两张表；本库现在没有自己的表。"""
    async with sessions() as s:
        names = set(
            (
                await s.execute(
                    sa.text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_name IN ('app_tenant_state', 'app_ops_request')"
                    )
                )
            ).scalars()
        )
    assert names == set()
