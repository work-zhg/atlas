"""技能应用测试的夹具。

★ 用独立的测试库 atlas_skill_test，不碰开发库 atlas_skill：运行时那边的
  clean_db 清的就是开发库，开发数据被测试清掉的事已经发生过。
★ 对象存储用 moto 的内存桶，不读 .env 里的真桶。
★ 多租户：每个用例默认在 TEST_TENANT 里（_in_test_tenant）；HTTP 请求由应用自己按请求头进入租户。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from uuid import UUID

import pytest
from atlas_skill.db.session import get_engine, get_sessionmaker
from atlas_skill.settings import get_settings
from atlas_tenancy import TenantScope
from skill_testkit import OPS_KEY, TEST_TENANT
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DB = "atlas_skill_test"
_ADMIN_URL = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas"
TEST_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{TEST_DB}"
_TABLES = (
    "skill",
    "skill_version",
    "skill_audit",
    "market_skill",
    "market_skill_version",
    "app_tenant_state",
    "app_ops_request",
)


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> Iterator[None]:
    overrides = {
        "ATLAS_SKILL_DATABASE_URL": TEST_URL,
        "ATLAS_SKILL_OPS_API_KEY": OPS_KEY,
        # 测试库里有多租户之前留下的行时，迁移 c0005 把它们归到测试租户
        "TENANT_BACKFILL_UUID": TEST_TENANT,
    }
    saved = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
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
    from atlas_skill.__main__ import migrate

    migrate()
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _in_test_tenant() -> Iterator[None]:
    """直接调 service 的用例默认在测试租户里（应用的每个请求会先清掉它，再按请求头进入）。"""
    with TenantScope.enter(UUID(TEST_TENANT)):
        yield


#: 用户中心授权快照的替身：没有免鉴权模式（dev 模式已删），谁都能用、操作码全有
ANYONE = {
    "can_access": True,
    "permissions": ["skill:upload", "skill:review", "audit:view"],
}


@pytest.fixture(autouse=True)
def _fake_user_center(monkeypatch: pytest.MonkeyPatch) -> None:
    """授权快照走替身：人都在请求头写的租户里。

    测权限的用例（test_auth_uc.py）自己再换 auth._transport。
    """
    import httpx
    from atlas_skill import auth

    def handler(request: httpx.Request) -> httpx.Response:
        user = {"status": "active", "tenant_id": request.headers.get("x-tenant-id")}
        return httpx.Response(200, json={**ANYONE, "user": user})

    monkeypatch.setattr(auth, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(auth, "_cache", auth._Cache())


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
    from atlas_skill.storage import S3SkillStorage
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        yield S3SkillStorage(client, "test-bucket")


@pytest.fixture
def settings():  # type: ignore[no-untyped-def]
    return get_settings()
