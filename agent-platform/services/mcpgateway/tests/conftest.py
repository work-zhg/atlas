"""MCP 网关测试的夹具。

★ 独立测试库 atlas_mcpgateway_test，不碰开发库。
★ 用户中心是替身：数据权限用内存实现（FakePermissions），授权快照用 httpx.MockTransport。
★ Higress 是替身（fake_higress.py），在后台线程里起 uvicorn；网关经真实的 HTTP 调它。
★ 网关本身经 ASGITransport 跑在测试的 event loop 里（库连接绑定 event loop）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fake_higress import FakeHigress, serve_in_thread
from gateway_testkit import ADMIN, FakePermissions
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DB = "atlas_mcpgateway_test"
_ADMIN_URL = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas"
TEST_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{TEST_DB}"
_TABLES = (
    "mcp_service",
    "mcp_tool",
    "mcp_tool_review",
    "mcp_api_key",
    "mcp_access_log",
    "mcp_audit",
)
CREDENTIAL = "gw-test-credential"

# 本机的服务不走代理：开着 HTTP(S)_PROXY 时，httpx2（FastMCP）会把发往 127.0.0.1 的请求也送进代理
os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(
    filter(None, [os.environ.get("NO_PROXY"), "127.0.0.1", "localhost", "gateway"])
)

_HIGRESS = FakeHigress(credential=CREDENTIAL)


@pytest.fixture(scope="session", autouse=True)
def _environment() -> Iterator[None]:
    from atlas_mcpgateway.settings import get_settings

    with serve_in_thread(_HIGRESS.app()) as higress_url:
        overrides = {
            "ATLAS_MCPGW_DATABASE_URL": TEST_URL,
            "ATLAS_MCPGW_HIGRESS_BASE_URL": higress_url,
            "ATLAS_MCPGW_GATEWAY_CREDENTIAL": CREDENTIAL,
            "ATLAS_MCPGW_MAINTAINER_TICK_S": "0",
            "ATLAS_MCPGW_MANUAL_REFRESH_MIN_INTERVAL_S": "0",
            "ATLAS_MCPGW_UC_BASE_URL": "http://uc.invalid",
            "ATLAS_MCPGW_DISCOVERY_TIMEOUT_S": "5",
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
        from atlas_mcpgateway.__main__ import migrate

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
    from atlas_mcpgateway.db.session import get_engine, get_sessionmaker

    async with get_sessionmaker()() as session:
        await session.execute(text(f"TRUNCATE {', '.join(_TABLES)}"))
        await session.commit()
    yield
    # 引擎绑定 event loop；每个测试一个 loop，所以每个测试结束都要重建
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
def higress() -> Iterator[FakeHigress]:
    _HIGRESS.services.clear()
    _HIGRESS.rejected.clear()
    yield _HIGRESS
    _HIGRESS.services.clear()


@pytest.fixture
def perms() -> FakePermissions:
    return FakePermissions()


@pytest.fixture(autouse=True)
def _fake_authz(monkeypatch: pytest.MonkeyPatch) -> None:
    """管理接口的授权快照：ADMIN 有 mcp:manage，其他人没有。"""
    from atlas_mcpgateway import auth

    def handler(request: httpx.Request) -> httpx.Response:
        uid = request.url.path.split("/")[-2]
        ops = ["mcp:manage"] if uid == ADMIN else []
        return httpx.Response(
            200, json={"can_access": True, "permissions": ops, "user": {"status": "active"}}
        )

    monkeypatch.setattr(auth, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(auth, "_cache", auth._Cache())


@pytest.fixture
async def app(perms: FakePermissions) -> AsyncIterator[Any]:
    from atlas_mcpgateway.api.app import create_app

    application = create_app(perms=perms)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as c:
        yield c
