"""模型网关测试的夹具。

★ 独立测试库 atlas_modelgateway_test，不碰开发库。
★ 用户中心是替身：数据权限用内存实现（FakePermissions），授权快照用 httpx.MockTransport。
★ Higress 是替身（fake_llm.py），在后台线程里起 uvicorn；网关经真实的 HTTP 调它。
★ 网关本身经 ASGITransport 跑在测试的 event loop 里（库连接绑定 event loop）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fake_llm import FakeLLM, serve_in_thread
from modelgw_testkit import ADMIN, FakePermissions
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DB = "atlas_modelgateway_test"
_ADMIN_URL = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas"
TEST_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{TEST_DB}"
_TABLES = (
    "gw_model",
    "gw_model_price",
    "gw_model_probe",
    "gw_purpose",
    "gw_model_key",
    "gw_model_call",
    "gw_budget",
    "gw_audit",
)
CREDENTIAL = "modelgw-test-credential"

# 本机的服务不走代理：开着 HTTP(S)_PROXY 时，发往 127.0.0.1 的请求也可能被送进代理
os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(
    filter(None, [os.environ.get("NO_PROXY"), "127.0.0.1", "localhost", "gateway"])
)

_LLM = FakeLLM(credential=CREDENTIAL)


@pytest.fixture(scope="session", autouse=True)
def _environment() -> Iterator[None]:
    from atlas_modelgateway.settings import get_settings

    with serve_in_thread(_LLM.app()) as higress_url:
        overrides = {
            "ATLAS_MODELGW_DATABASE_URL": TEST_URL,
            "ATLAS_MODELGW_HIGRESS_BASE_URL": higress_url,
            "ATLAS_MODELGW_GATEWAY_CREDENTIAL": CREDENTIAL,
            "ATLAS_MODELGW_MAINTAINER_TICK_S": "0",
            "ATLAS_MODELGW_UC_BASE_URL": "http://uc.invalid",
            "ATLAS_MODELGW_PROBE_TIMEOUT_S": "10",
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
def llm() -> Iterator[FakeLLM]:
    _LLM.models.clear()
    _LLM.seen.clear()
    yield _LLM
    _LLM.models.clear()


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
async def app(perms: FakePermissions) -> AsyncIterator[Any]:
    from atlas_modelgateway.api.app import create_app

    application = create_app(perms=perms)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as c:
        yield c
