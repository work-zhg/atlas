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
from memory_testkit import FakePermissions
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DB = "atlas_memory_test"
_ADMIN_URL = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas"
TEST_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{TEST_DB}"
_TABLES = ("mem_scope", "mem_instance", "mem_topic", "mem_entry", "mem_revision")


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
    """作用域管理接口的授权快照：用户 admin 有 memory_scope:manage，其他人没有。"""
    from atlas_memory import auth

    def handler(request: httpx.Request) -> httpx.Response:
        uid = request.url.path.split("/")[-2]
        ops = ["memory_scope:manage"] if uid == "admin" else []
        return httpx.Response(
            200, json={"can_access": True, "permissions": ops, "user": {"status": "active"}}
        )

    monkeypatch.setattr(auth, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(auth, "_cache", auth._Cache())


@pytest.fixture
async def client(perms: FakePermissions) -> AsyncIterator[httpx.AsyncClient]:
    from atlas_memory.api.app import create_app

    app = create_app(perms=perms)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://memory"
    ) as c:
        yield c
