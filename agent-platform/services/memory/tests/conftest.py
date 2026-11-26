"""记忆服务测试的夹具。

★ 独立测试库 atlas_memory_test，不碰开发库（运行时那边的 clean_db 清掉过开发数据）。
★ 用户中心是替身：数据权限用内存实现（FakePermissions），授权快照用 httpx.MockTransport。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from atlas_memory.db.session import get_engine, get_sessionmaker
from atlas_memory.settings import get_settings
from memory_testkit import BLOCKED_TENANTS, TEST_TENANT, USERS_ELSEWHERE, FakePermissions
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DB = "atlas_memory_test"
_ADMIN_URL = "postgresql+asyncpg://atlas:atlas@localhost:5433/postgres"
TEST_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{TEST_DB}"
_TABLES = (
    "mem_scope",
    "mem_instance",
    "mem_topic",
    "mem_entry",
    "mem_revision",
)
OPS_KEY = "test-ops-key"


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> Iterator[None]:
    saved = os.environ.get("ATLAS_MEMORY_DATABASE_URL")
    os.environ["ATLAS_MEMORY_DATABASE_URL"] = TEST_URL
    get_settings.cache_clear()

    async def _ensure() -> None:
        engine = create_async_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEST_DB}
            )
            if not exists:
                await conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
        await engine.dispose()

    asyncio.run(_ensure())
    from atlas_memory.__main__ import migrate

    migrate()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("ATLAS_MEMORY_DATABASE_URL", None)
        else:
            os.environ["ATLAS_MEMORY_DATABASE_URL"] = saved
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _clean_tables() -> AsyncIterator[None]:
    async with get_sessionmaker()() as session:
        await session.execute(text(f"TRUNCATE {', '.join(_TABLES)}"))
        await session.commit()
    yield
    # 引擎绑定 event loop；每个测试一个 loop，所以每个测试结束都要重建
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
def perms() -> FakePermissions:
    return FakePermissions()


@pytest.fixture(autouse=True)
def _fake_authz(monkeypatch: pytest.MonkeyPatch) -> None:
    """用户中心的授权快照：用户都在请求头写的租户里，除了 users_elsewhere 里登记过的（用户中心
    在请求头的租户里找不到他 → 404）。"""
    from atlas_memory import auth

    def handler(request: httpx.Request) -> httpx.Response:
        uid = request.url.path.split("/")[-2]
        tenant = request.headers.get("x-tenant-id")
        home = USERS_ELSEWHERE.get(uid)
        if home is not None and home != tenant:
            return httpx.Response(404, json={"message": "用户不存在"})
        return httpx.Response(
            200,
            json={
                "can_access": tenant not in BLOCKED_TENANTS,
                "permissions": [],
                "user": {"id": uid, "status": "active", "tenant_id": tenant},
            },
        )

    monkeypatch.setattr(auth, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(auth, "_cache", auth._Cache())


@pytest.fixture(autouse=True)
def _ops_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_MEMORY_OPS_API_KEY", OPS_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    USERS_ELSEWHERE.clear()
    BLOCKED_TENANTS.clear()


@pytest.fixture
async def client(perms: FakePermissions) -> AsyncIterator[httpx.AsyncClient]:
    from atlas_memory.api.app import create_app

    app = create_app(perms=perms)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://memory",
        headers={"X-Tenant-Id": TEST_TENANT},
    ) as c:
        yield c
