"""TeamFlow 测试夹具。

★ 独立测试库 atlas_teamflow_test（不碰开发库）；用户中心与 Atlas 用内存实现（tf_fakes）。
★ 每个用例前清空全部 tf_ 表。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import pytest
from atlas_teamflow.db.models import Base
from atlas_teamflow.db.session import get_engine, get_sessionmaker
from atlas_teamflow.settings import get_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DB = "atlas_teamflow_test"
_ADMIN_URL = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas"
TEST_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{TEST_DB}"


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> Iterator[None]:
    saved = os.environ.get("ATLAS_TF_DATABASE_URL")
    os.environ["ATLAS_TF_DATABASE_URL"] = TEST_URL
    # ★ 测试绝不碰真实外部服务：.env 里配的 Gitee / Redis 一律覆盖掉
    #   （环境变量优先于 .env）。需要 Redis 的用例自己构造协调器。
    os.environ["ATLAS_TF_GIT_BACKEND"] = "local"
    os.environ["ATLAS_TF_GITEE_TOKEN"] = ""
    os.environ["ATLAS_TF_REDIS_URL"] = ""
    os.environ["ATLAS_TF_EMBEDDED_WORKER"] = "true"
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
    from atlas_teamflow.__main__ import migrate

    migrate()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    try:
        yield
    finally:
        for k in (
            "ATLAS_TF_GIT_BACKEND",
            "ATLAS_TF_GITEE_TOKEN",
            "ATLAS_TF_REDIS_URL",
            "ATLAS_TF_EMBEDDED_WORKER",
        ):
            os.environ.pop(k, None)
        if saved is None:
            os.environ.pop("ATLAS_TF_DATABASE_URL", None)
        else:
            os.environ["ATLAS_TF_DATABASE_URL"] = saved
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _clean() -> AsyncIterator[None]:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with get_sessionmaker()() as session:
        await session.execute(text(f"TRUNCATE {tables} CASCADE"))
        await session.commit()
    yield
    await get_engine().dispose()
