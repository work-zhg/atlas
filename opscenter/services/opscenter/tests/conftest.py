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
_ADMIN_URL = "postgresql+asyncpg://atlas:atlas@localhost:5433/postgres"
OPS_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{OPS_DB}"
UC_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{UC_DB}"
KEYS = {"usercenter": "key-uc", "alpha": "key-alpha", "beta": "key-beta"}
#: 测试里登记的系统各自的运营模块
MODULES = {"usercenter": "OpsUserCenter", "alpha": "OpsAlpha", "beta": "OpsBeta"}
ALL_MODULES = ("OpsTenant", *MODULES.values())


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
    """一个声明了租户生命周期的业务系统（运营接口库的契约：创建、删除）：验签名，记下被调了什么。"""

    code: str
    calls: list[tuple[str, str]] = field(default_factory=list)
    #: 某个动作先失败几次（503）：create / delete
    fail: dict[str, int] = field(default_factory=dict)
    #: 某个动作被明确拒绝（409）
    reject: set[str] = field(default_factory=set)
    #: 删除要几批才删完
    purge_batches: int = 1
    #: 创建时收到的参数：{租户: 参数}
    created: dict[str, dict[str, Any]] = field(default_factory=dict)
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

        async def _called(action: str, tenant: UUID, request: Request) -> None:
            stamp = await guard.verify(request)
            self.calls.append((action, str(tenant)))
            self.idempotency.append(str(stamp.idempotency_key) if stamp.idempotency_key else None)
            _check(action)

        @app.put("/ops/v1/tenants/{tenant}")
        async def create(tenant: UUID, request: Request) -> dict[str, Any]:
            await _called("create", tenant, request)
            self.created[str(tenant)] = await request.json()
            return {"tenant_uuid": str(tenant)}

        @app.delete("/ops/v1/tenants/{tenant}")
        async def delete(tenant: UUID, request: Request) -> dict[str, Any]:
            await _called("delete", tenant, request)
            left = sum(1 for a, _ in self.calls if a == "delete") < self.purge_batches
            return {"deleted": {"rows": 10}, "remaining": left}

        @app.put("/ops/v1/quotas/{tenant}")
        async def set_quota(tenant: UUID, request: Request) -> dict[str, Any]:
            await guard.verify(request)
            self.quotas[str(tenant)] = await request.json()
            return {"tenant_uuid": str(tenant)}

        @app.get("/ops/v1/capacity")
        async def capacity(request: Request) -> dict[str, Any]:
            await guard.verify(request)
            return {"usage": {"storage_bytes": 1}}

        @app.get("/ops/v1/echo/{rest:path}")
        async def echo(rest: str, request: Request) -> dict[str, Any]:
            """验签名后回显路径：签名两边的路径编码要对得上（中文、空格）。"""
            await guard.verify(request)
            return {"rest": rest, "query": dict(request.query_params)}

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


@pytest.fixture
async def world() -> AsyncIterator[World]:
    from atlas_opscenter.api.app import create_app
    from atlas_opscenter.db.session import get_sessionmaker
    from atlas_opscenter.settings import OpsSettings, SystemConfig
    from atlas_opscenter.tenants import OperationSweeper, StepRunner
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
                module_code=MODULES["usercenter"],
            ),
            SystemConfig(
                code="alpha",
                name="甲",
                base_url="http://alpha",
                capabilities=["lifecycle", "capacity"],
                module_code=MODULES["alpha"],
            ),
            SystemConfig(
                code="beta",
                name="乙",
                base_url="http://beta",
                capabilities=["lifecycle"],
                module_code=MODULES["beta"],
            ),
        ],
        system_keys=KEYS,
        cors_origins=[],
    )
    transport = Router({"uc": uc_app(), "alpha": alpha.app(), "beta": beta.app()})
    app = create_app(settings=settings, transport=transport, background=False)
    client = app.state.ops_client
    sweeper = OperationSweeper(get_sessionmaker(), StepRunner(client), settings.lease_s)
    yield World(app, alpha, beta, sweeper)
    await client.aclose()


class Console:
    """模拟经 API 网关进来的运营控制台：带网关验过访问 JWT 后写的身份头。

    登录、CSRF 都在网关和用户中心，运营中心不管。
    """

    def __init__(self, app: Any, user: UUID | None = None, tenant: UUID | None = None) -> None:
        headers = {"X-Caller-App": "gateway"}
        if user is not None and tenant is not None:
            headers |= {"X-Uid": str(user), "X-Tenant-Id": str(tenant)}
        self.user, self.tenant = user, tenant
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://ops", headers=headers
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.http.get(url, **kw)

    async def send(self, method: str, url: str, json: Any = None) -> httpx.Response:
        return await self.http.request(method, url, json=json)

    async def post(self, url: str, json: Any = None) -> httpx.Response:
        return await self.send("POST", url, json)


async def system_tenant() -> tuple[UUID, UUID]:
    """用户中心里的系统租户（platform）和它的首个管理员 → (租户, 管理员)。已经有了就直接返回。"""
    from atlas_usercenter.bootstrap import bootstrap_system
    from atlas_usercenter.db.models import Tenant, User
    from atlas_usercenter.db.session import get_sessionmaker as uc_sessions
    from atlas_usercenter.db.tenancy import platform_scope
    from sqlalchemy import select

    async with uc_sessions()() as s:
        with platform_scope(s):
            t = await s.scalar(select(Tenant).where(Tenant.is_system.is_(True)))
        if t is None:
            await bootstrap_system(
                s, code="platform", company="平台运营", account="ops", name="值班", email="o@p.com"
            )
            await s.commit()
            with platform_scope(s):
                t = await s.scalar(select(Tenant).where(Tenant.is_system.is_(True)))
        with platform_scope(s):
            admin = await s.scalar(select(User.uuid).where(User.uuid == t.admin_uuid))
        return t.uuid, admin


async def uc_user(tenant: UUID, account: str) -> UUID:
    """在用户中心的某个租户里建一个人。"""
    from atlas_usercenter.audit import Actor
    from atlas_usercenter.db.session import get_sessionmaker as uc_sessions
    from atlas_usercenter.db.tenancy import tenant_scope
    from atlas_usercenter.org.service import OrgService
    from atlas_usercenter.settings import get_settings as uc_settings
    from atlas_usercenter.user.service import UserService

    async with uc_sessions()() as s:
        with tenant_scope(s, tenant):
            root = await OrgService(s, Actor.system()).root()
            assert root is not None
            user, _temp = await UserService(s, Actor.system(), uc_settings()).create(
                {
                    "name": account,
                    "account": account,
                    "email": f"{account}@p.com",
                    "dept_uuid": root.uuid,
                }
            )
            await s.commit()
            return user.uuid


async def grant(tenant: UUID, user: UUID, levels: dict[str, str], *, auditor: bool = False) -> None:
    """在用户中心给人授运营模块（数据编码的「全部」级别），auditor 再授内置角色「运营审计员」。"""
    from atlas_usercenter.db.models import DataType, DataTypeAcl, Grant, Role
    from atlas_usercenter.db.session import get_sessionmaker as uc_sessions
    from atlas_usercenter.db.tenancy import platform_scope, tenant_scope
    from sqlalchemy import select

    rank = {"READ": 1, "WRITE": 2, "OWNER": 3}
    async with uc_sessions()() as s:
        with platform_scope(s):
            types = {d.code: d.uuid for d in await s.scalars(select(DataType))}
            role = await s.scalar(select(Role.uuid).where(Role.code == "OPS_AUDITOR"))
        with tenant_scope(s, tenant):
            for code, level in levels.items():
                s.add(
                    DataTypeAcl(
                        data_type_uuid=types[code],
                        level=rank[level],
                        subject_type="user",
                        user_uuid=user,
                    )
                )
            if auditor:
                s.add(Grant(kind="role", role_uuid=role, subject_type="user", user_uuid=user))
            await s.commit()


async def operator(
    app: Any, levels: dict[str, str], *, account: str | None = None, auditor: bool = False
) -> Console:
    """系统租户里的一个运营人员（account 为空 = 首个管理员），授好模块后的控制台。"""
    tenant, admin = await system_tenant()
    await app.state.catalog_sync.run()  # 运营中心的应用目录同步进用户中心，并给系统租户开通
    user = await uc_user(tenant, account) if account else admin
    await grant(tenant, user, levels, auditor=auditor)
    return Console(app, user, tenant)


async def admin_console(app: Any) -> Console:
    """全部模块 Owner、能看审计的运营负责人（系统租户的首个管理员）。"""
    return await operator(app, dict.fromkeys(ALL_MODULES, "OWNER"), auditor=True)
