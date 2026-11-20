"""运营中心测试夹具。

★ 运营库用独立的测试库 atlas_opscenter_test；用户中心是真的（进程内的 ASGI 应用，测试库
  atlas_usercenter_test，和用户中心自己的测试共用），验签名、建租户都走真实契约。
★ 另有两个假的业务系统 alpha（租户生命周期 + 容量）、beta（租户生命周期）：用 atlas_opsapi 的
  OpsGuard 验签名，记下被调了什么，可以让某一步失败几次、删除分几批。
★ 后台任务不起（background=False）：测试里手动 sweeper.tick()，时间也由测试拨。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
import pytest
from atlas_opsapi import OpsGuard
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

OPS_DB = "atlas_opscenter_test"
UC_DB = "atlas_usercenter_test"
_ADMIN_URL = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas"
OPS_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{OPS_DB}"
UC_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{UC_DB}"
KEYS = {"usercenter": "key-uc", "alpha": "key-alpha", "beta": "key-beta"}
STRONG = "OpsPassw0rd!"


@pytest.fixture(scope="session", autouse=True)
def _databases() -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in ("ATLAS_OPS_DATABASE_URL", "ATLAS_UC_DATABASE_URL")}
    os.environ["ATLAS_OPS_DATABASE_URL"] = OPS_URL
    os.environ["ATLAS_UC_DATABASE_URL"] = UC_URL
    os.environ["ATLAS_UC_OPS_API_KEY"] = KEYS["usercenter"]

    async def _ensure() -> None:
        engine = create_async_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            for db in (OPS_DB, UC_DB):
                if not await conn.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db}
                ):
                    await conn.execute(text(f"CREATE DATABASE {db}"))
        await engine.dispose()

    asyncio.run(_ensure())
    from atlas_opscenter.__main__ import migrate as ops_migrate
    from atlas_opscenter.settings import get_settings as ops_settings
    from atlas_usercenter.__main__ import migrate as uc_migrate
    from atlas_usercenter.settings import get_settings as uc_settings

    ops_settings.cache_clear()
    uc_settings.cache_clear()
    ops_migrate()
    uc_migrate()
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ops_settings.cache_clear()
        uc_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _clean() -> AsyncIterator[None]:
    from atlas_opscenter.db.models import Base as OpsBase
    from atlas_opscenter.db.session import get_engine as ops_engine
    from atlas_opscenter.db.session import get_sessionmaker as ops_sessions
    from atlas_usercenter.bootstrap import bootstrap
    from atlas_usercenter.db.models import Base as UcBase
    from atlas_usercenter.db.session import get_engine as uc_engine
    from atlas_usercenter.db.session import get_sessionmaker as uc_sessions

    for f in (ops_engine, ops_sessions, uc_engine, uc_sessions):
        f.cache_clear()
    async with ops_sessions()() as s:
        await s.execute(
            text(f"TRUNCATE {', '.join(t.name for t in OpsBase.metadata.sorted_tables)}")
        )
        await s.commit()
    async with uc_sessions()() as s:
        await s.execute(
            text(f"TRUNCATE {', '.join(t.name for t in UcBase.metadata.sorted_tables)} CASCADE")
        )
        await s.commit()
        await bootstrap(s, company="星海科技", account="admin", name="管理员", email="a@x.com")
        await s.commit()
    yield
    await ops_engine().dispose()
    await uc_engine().dispose()


# ───────────────────────────── 假的业务系统


@dataclass
class FakeSystem:
    """一个声明了租户生命周期的业务系统：验签名，记下被调了什么。"""

    code: str
    calls: list[tuple[str, str]] = field(default_factory=list)
    #: 某个动作先失败几次（503）
    fail: dict[str, int] = field(default_factory=dict)
    #: 某个动作被明确拒绝（409）
    reject: set[str] = field(default_factory=set)
    #: 删除要几批才删完
    purge_batches: int = 1
    quotas: dict[str, dict[str, Any]] = field(default_factory=dict)
    idempotency: list[str | None] = field(default_factory=list)

    def app(self) -> FastAPI:
        app = FastAPI()
        guard = OpsGuard(KEYS[self.code])

        def _check(action: str) -> None:
            if action in self.reject:
                raise HTTPException(409, f"{self.code} 不同意 {action}")
            if self.fail.get(action, 0) > 0:
                self.fail[action] -= 1
                raise HTTPException(503, f"{self.code} 暂时不可用")

        @app.post("/ops/v1/tenants/{tenant}/{action}")
        async def lifecycle(tenant: UUID, action: str, request: Request) -> dict[str, Any]:
            stamp = await guard.verify(request)
            self.calls.append((action, str(tenant)))
            self.idempotency.append(str(stamp.idempotency_key) if stamp.idempotency_key else None)
            _check(action)
            if action == "purge":
                left = sum(1 for a, _ in self.calls if a == "purge") < self.purge_batches
                return {"deleted": {"rows": 10}, "remaining": left}
            return {"status": action}

        @app.put("/ops/v1/quotas/{tenant}")
        async def set_quota(tenant: UUID, request: Request) -> dict[str, Any]:
            await guard.verify(request)
            self.calls.append(("set_quota", str(tenant)))
            _check("set_quota")
            self.quotas[str(tenant)] = await request.json()
            return {"tenant_uuid": str(tenant)}

        @app.get("/ops/v1/capacity")
        async def capacity(request: Request) -> dict[str, Any]:
            await guard.verify(request)
            return {"usage": {"storage_bytes": 1}}

        return app


class Router(httpx.AsyncBaseTransport):
    """按主机名把请求交给对应的进程内应用。"""

    def __init__(self, apps: dict[str, Any]) -> None:
        self._t = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._t[request.url.host].handle_async_request(request)


@dataclass
class World:
    ops: Any  # FastAPI
    alpha: FakeSystem
    beta: FakeSystem
    sweeper: Any
    expiry: Any


@pytest.fixture
async def world() -> AsyncIterator[World]:
    from atlas_opscenter.api.app import create_app
    from atlas_opscenter.db.session import get_sessionmaker
    from atlas_opscenter.settings import OpsSettings, SystemConfig
    from atlas_opscenter.tenants import ExpiryWatcher, OperationSweeper, StepRunner
    from atlas_usercenter.api.app import create_app as uc_app
    from atlas_usercenter.settings import get_settings as uc_settings

    # 同一个 pytest 进程里用户中心自己的测试也会设它：建应用前再设一次
    os.environ["ATLAS_UC_OPS_API_KEY"] = KEYS["usercenter"]
    uc_settings.cache_clear()
    alpha, beta = FakeSystem("alpha"), FakeSystem("beta")
    settings = OpsSettings(
        database_url=OPS_URL,
        systems=[
            SystemConfig(
                code="usercenter",
                name="用户中心",
                base_url="http://uc",
                capabilities=["user_center"],
            ),
            SystemConfig(
                code="alpha",
                name="甲",
                base_url="http://alpha",
                capabilities=["lifecycle", "capacity"],
            ),
            SystemConfig(
                code="beta", name="乙", base_url="http://beta", capabilities=["lifecycle"]
            ),
        ],
        system_keys=KEYS,
        cors_origins=[],
    )
    transport = Router({"uc": uc_app(), "alpha": alpha.app(), "beta": beta.app()})
    app = create_app(settings=settings, transport=transport, background=False)
    client = app.state.ops_client
    sweeper = OperationSweeper(get_sessionmaker(), StepRunner(client), settings.lease_s)
    expiry = ExpiryWatcher(get_sessionmaker(), client)
    yield World(app, alpha, beta, sweeper, expiry)
    await client.aclose()


class Console:
    """模拟运营控制台：Cookie 由 httpx 保存，写请求自动带 CSRF 头。"""

    def __init__(self, app: Any) -> None:
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ops")

    async def close(self) -> None:
        await self.http.aclose()

    def _h(self) -> dict[str, str]:
        csrf = self.http.cookies.get("ops_csrf")
        return {"X-Ops-CSRF": csrf} if csrf else {}

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.http.get(url, **kw)

    async def send(self, method: str, url: str, json: Any = None) -> httpx.Response:
        return await self.http.request(method, url, json=json, headers=self._h())

    async def post(self, url: str, json: Any = None) -> httpx.Response:
        return await self.send("POST", url, json)

    async def login(self, account: str, password: str) -> httpx.Response:
        return await self.http.post(
            "/api/auth/login", json={"account": account, "password": password}
        )


async def admin_console(app: Any, account: str = "ops") -> Console:
    """bootstrap 第一个运营管理员，登录并改掉临时密码。"""
    from atlas_opscenter.accounts import Accounts
    from atlas_opscenter.db.session import get_sessionmaker
    from atlas_opscenter.settings import get_settings

    async with get_sessionmaker()() as s:
        temp = await Accounts(s, get_settings()).bootstrap(account, "值班")
        await s.commit()
    c = Console(app)
    assert (await c.login(account, temp)).status_code == 200
    r = await c.post("/api/auth/password", {"old_password": temp, "new_password": STRONG})
    assert r.status_code == 200, r.text
    return c
