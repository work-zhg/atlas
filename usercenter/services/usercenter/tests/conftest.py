"""用户中心测试夹具。

★ 用独立的测试库 atlas_usercenter_test，不碰开发库 atlas_usercenter（运行时那边的
  clean_db 清掉开发数据的事已经发生过）。
★ 每个用例前清空全部 uc_ 表、同步内置应用、bootstrap 一个超级管理员。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import pytest
from atlas_usercenter.db.models import Base
from atlas_usercenter.db.session import get_engine, get_sessionmaker
from atlas_usercenter.settings import get_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DB = "atlas_usercenter_test"
_ADMIN_URL = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas"
TEST_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{TEST_DB}"


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> Iterator[None]:
    saved = os.environ.get("ATLAS_UC_DATABASE_URL")
    os.environ["ATLAS_UC_DATABASE_URL"] = TEST_URL
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
    from atlas_usercenter.__main__ import migrate

    migrate()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("ATLAS_UC_DATABASE_URL", None)
        else:
            os.environ["ATLAS_UC_DATABASE_URL"] = saved
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _clean() -> AsyncIterator[None]:
    from atlas_usercenter.bootstrap import bootstrap

    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with get_sessionmaker()() as session:
        await session.execute(text(f"TRUNCATE {tables} CASCADE"))
        await session.commit()
        _account, temp = await bootstrap(
            session, company="星海科技", account="admin", name="管理员", email="admin@xinghai.com"
        )
        await session.commit()
    os.environ["UC_TEST_ADMIN_TEMP"] = temp
    yield
    await get_engine().dispose()
