"""配置服务测试的夹具。

★ 用独立的测试库 atlas_config_test，不碰开发库 atlas_config：运行时那边的
  clean_db 清的就是开发库，开发数据被测试清掉的事已经发生过。
★ 对象存储用 moto 的内存桶，不读 .env 里的真桶。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import pytest
from atlas_config.db.session import get_engine, get_sessionmaker
from atlas_config.settings import get_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DB = "atlas_config_test"
_ADMIN_URL = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas"
TEST_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{TEST_DB}"
_TABLES = ("skill", "skill_version", "mcp_server", "mcp_tool_review", "config_audit")


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> Iterator[None]:
    saved = os.environ.get("ATLAS_CONFIG_DATABASE_URL")
    os.environ["ATLAS_CONFIG_DATABASE_URL"] = TEST_URL
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
    from atlas_config.__main__ import migrate

    migrate()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("ATLAS_CONFIG_DATABASE_URL", None)
        else:
            os.environ["ATLAS_CONFIG_DATABASE_URL"] = saved
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _clean_tables() -> AsyncIterator[None]:
    async with get_sessionmaker()() as session:
        await session.execute(text(f"TRUNCATE {', '.join(_TABLES)}"))
        await session.commit()
    yield
    # 引擎绑定 event loop；每个测试一个 loop，所以每个测试结束都要重建（同运行时 conftest）
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
def storage() -> Iterator[object]:
    import boto3
    from atlas_config.storage import S3SkillStorage
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        yield S3SkillStorage(client, "test-bucket")


@pytest.fixture
def settings():  # type: ignore[no-untyped-def]
    return get_settings()
