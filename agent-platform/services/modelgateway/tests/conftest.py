"""模型管理平台测试的夹具。

★ 独立测试库 atlas_modelgateway_test，不碰开发库。
★ 用户中心是替身：数据权限用内存实现（FakePermissions），授权快照用 httpx.MockTransport。
★ LiteLLM 是替身（FakeLiteLLM），经 httpx.MockTransport 在进程内应答，不起端口。
★ 平台本身经 ASGITransport 跑在测试的 event loop 里（库连接绑定 event loop）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from modelgw_testkit import ADMIN, MASTER_KEY, FakeLiteLLM, FakePermissions
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DB = "atlas_modelgateway_test"
_ADMIN_URL = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas"
TEST_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{TEST_DB}"
_TABLES = ("gw_model", "gw_api_key", "gw_audit")


@pytest.fixture(scope="session", autouse=True)
def _environment() -> Iterator[None]:
    from atlas_modelgateway.settings import get_settings

    overrides = {
        "ATLAS_MODELGW_DATABASE_URL": TEST_URL,
        "ATLAS_MODELGW_LITELLM_BASE_URL": "http://litellm.test",
        "ATLAS_MODELGW_LITELLM_MASTER_KEY": MASTER_KEY,
        "ATLAS_MODELGW_RECONCILE_INTERVAL_S": "0",
        "ATLAS_MODELGW_UC_BASE_URL": "http://uc.invalid",
        # 测试里每次都看最新的：授权改了立刻算
        "ATLAS_MODELGW_PERM_CACHE_TTL_S": "0",
        "ATLAS_MODELGW_LITELLM_MODELS_TTL_S": "0",
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
    from atlas_modelgateway.__main__ import migrate

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
async def _clean_tables() -> AsyncIterator[None]:
    from atlas_modelgateway.db.session import get_engine, get_sessionmaker

    async with get_sessionmaker()() as session:
        await session.execute(text(f"TRUNCATE {', '.join(_TABLES)}"))
        await session.commit()
    yield
    # 引擎绑定 event loop；每个测试一个 loop，所以每个测试结束都要重建
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
def litellm() -> FakeLiteLLM:
    return FakeLiteLLM()


@pytest.fixture
def perms() -> FakePermissions:
    return FakePermissions()


@pytest.fixture(autouse=True)
def _fake_authz(monkeypatch: pytest.MonkeyPatch) -> None:
    """管理接口的授权快照：ADMIN 有 model:manage，其他人没有。"""
    from atlas_modelgateway import auth

    def handler(request: httpx.Request) -> httpx.Response:
        uid = request.url.path.split("/")[-2]
        ops = ["model:manage"] if uid == ADMIN else []
        return httpx.Response(
            200, json={"can_access": True, "permissions": ops, "user": {"status": "active"}}
        )

    monkeypatch.setattr(auth, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(auth, "_cache", auth._Cache())


@pytest.fixture
async def app(perms: FakePermissions, litellm: FakeLiteLLM) -> AsyncIterator[Any]:
    from atlas_modelgateway.api.app import create_app

    application = create_app(perms=perms, litellm_transport=litellm.transport())
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://platform"
    ) as c:
        yield c
