"""Atlas 测 MCP 用的环境：真实的 MCP 网关（进程内）+ Higress 替身（后台线程）。

MCP 网关用真实现：atlas_mcpgateway 的 app 经 ASGITransport 跑在测试的 event loop 里（它自己的
测试库 atlas_mcpgateway_test），用户中心换成内存替身；Higress 是后台线程里的替身（只认网关凭据）。
Atlas 的内部接口调用（GatewayClient）、MCP 客户端（FastMCP / httpx2）都经 ASGITransport 连到它。

用法（测试模块里）：

    from tests.gateway_env import gateway, gateway_environment, higress  # noqa: F401（夹具）
    pytestmark = pytest.mark.usefixtures("gateway_environment")

★ 网关测试里的替身（fake_higress、gateway_testkit）同一份，不复制。
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from atlas_agent.domain.events import EventType
from atlas_agent.providers.mcp import CallContext, KeyAuth, McpSessions, NativeMcpTools
from atlas_agent.providers.mcp import config as mcp_config
from atlas_agent.providers.mcp.gateway import (
    GatewayClient,
    gateway_catalog,
    reset_gateway_cache,
    subject_access,
)
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/mcpgateway/tests"))
from fake_higress import FakeHigress, FakeService, ToolFailure, serve_in_thread
from gateway_testkit import ADMIN, FakePermissions, as_user

__all__ = [
    "ADMIN",
    "ALICE",
    "DIGITAL",
    "GATEWAY",
    "FakeHigress",
    "FakePermissions",
    "FakeService",
    "ToolFailure",
    "approve",
    "as_user",
    "assemble",
    "call",
    "ctx",
    "gateway",
    "gateway_environment",
    "higress",
    "issue_key",
    "register",
    "text_of",
]

GATEWAY_DB = "atlas_mcpgateway_test"
_ADMIN_URL = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas"
_GATEWAY_DB_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{GATEWAY_DB}"
CREDENTIAL = "gw-atlas-test"
GATEWAY = "http://gateway"

ALICE = UUID("00000000-0000-7000-8000-0000000000a1")
DIGITAL = UUID("00000000-0000-7000-8000-0000000000de")

_HIGRESS = FakeHigress(credential=CREDENTIAL)


@pytest.fixture(scope="module")
def gateway_environment() -> Iterator[str]:
    """起 Higress 替身、准备网关的测试库与设置。→ Higress 替身的地址。"""
    from atlas_mcpgateway.settings import get_settings as gateway_settings
    from sqlalchemy.ext.asyncio import create_async_engine

    with serve_in_thread(_HIGRESS.app()) as higress_url:
        overrides = {
            "ATLAS_MCPGW_DATABASE_URL": _GATEWAY_DB_URL,
            "ATLAS_MCPGW_HIGRESS_BASE_URL": higress_url,
            "ATLAS_MCPGW_GATEWAY_CREDENTIAL": CREDENTIAL,
            "ATLAS_MCPGW_MAINTAINER_TICK_S": "0",
            "ATLAS_MCPGW_MANUAL_REFRESH_MIN_INTERVAL_S": "0",
            "ATLAS_MCPGW_UC_BASE_URL": "http://uc.invalid",
        }
        saved = {k: os.environ.get(k) for k in overrides}
        os.environ.update(overrides)
        gateway_settings.cache_clear()

        async def _ensure() -> None:
            engine = create_async_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
            async with engine.connect() as conn:
                exists = await conn.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": GATEWAY_DB}
                )
                if not exists:
                    await conn.execute(text(f"CREATE DATABASE {GATEWAY_DB}"))
            await engine.dispose()

        asyncio.run(_ensure())
        from atlas_mcpgateway.__main__ import migrate

        migrate()
        try:
            yield higress_url
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            gateway_settings.cache_clear()


@pytest.fixture
def higress() -> Iterator[FakeHigress]:
    """Higress 替身（每个测试清空）：服务由测试自己放进 higress.services。"""
    _HIGRESS.services.clear()
    _HIGRESS.rejected.clear()
    yield _HIGRESS
    _HIGRESS.services.clear()


@pytest.fixture
async def gateway(
    monkeypatch: pytest.MonkeyPatch, higress: FakeHigress
) -> AsyncIterator[tuple[Any, FakePermissions, httpx.AsyncClient]]:
    """进程内的 MCP 网关（空库）。→ (app, 用户中心替身, 管理接口的客户端)。"""
    import httpx2
    from atlas_mcpgateway import auth as gateway_auth
    from atlas_mcpgateway.api.app import create_app
    from atlas_mcpgateway.db.session import get_engine, get_sessionmaker

    def authz(request: httpx.Request) -> httpx.Response:
        uid = request.url.path.split("/")[-2]
        ops = ["mcp:manage"] if uid == ADMIN else []
        return httpx.Response(
            200, json={"can_access": True, "permissions": ops, "user": {"status": "active"}}
        )

    monkeypatch.setattr(gateway_auth, "_transport", httpx.MockTransport(authz))
    monkeypatch.setattr(gateway_auth, "_cache", gateway_auth._Cache())
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "TRUNCATE mcp_service, mcp_tool, mcp_tool_review, mcp_api_key,"
                " mcp_access_log, mcp_audit"
            )
        )
        await session.commit()

    perms = FakePermissions()
    app = create_app(perms=perms)
    reset_gateway_cache()
    async with app.router.lifespan_context(app):
        monkeypatch.setattr(GatewayClient, "transport", httpx.ASGITransport(app=app))
        monkeypatch.setattr(mcp_config, "_transport", httpx2.ASGITransport(app=app))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=GATEWAY
        ) as client:
            yield app, perms, client
    reset_gateway_cache()
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


# ───────────────────────────── 网关上的操作（管理接口）


async def register(
    c: httpx.AsyncClient,
    name: str,
    *,
    review: bool = False,
    call_timeout_s: float | None = None,
    higress: FakeHigress | None = None,
) -> None:
    r = await c.post(
        "/api/v1/services",
        json={"name": name, "review_required": review, "call_timeout_s": call_timeout_s},
        headers=as_user(ADMIN),
    )
    assert r.status_code == 201, r.text
    if higress is not None:
        # 登记时网关自己拉过一次工具定义：之后只看 Atlas 发出的请求
        higress.services[name].seen.clear()


async def approve(c: httpx.AsyncClient, service: str, tool: str) -> None:
    detail = (await c.get(f"/api/v1/services/{service}", headers=as_user(ADMIN))).json()
    digest = next(t["digest"] for t in detail["tools"] if t["name"] == tool)
    r = await c.post(
        f"/api/v1/services/{service}/tools/{tool}/review",
        json={"digest": digest, "decision": "approved"},
        headers=as_user(ADMIN),
    )
    assert r.status_code == 200, r.text


async def issue_key(c: httpx.AsyncClient, subject: UUID) -> None:
    r = await c.post("/api/v1/keys", json={"subject_uid": str(subject)}, headers=as_user(ADMIN))
    assert r.status_code == 201, r.text


# ───────────────────────────── Atlas 侧：装配、调用


def ctx(user: UUID = ALICE, auth: UUID | None = None) -> CallContext:
    return CallContext(run_id=uuid4(), thread_id=uuid4(), user_id=user, auth_uid=auth)


async def assemble(
    tool_names: list[str],
    subject: UUID = ALICE,
    *,
    call_timeout_s: float = 30,
    max_result_chars: int = 100_000,
    call_ctx: CallContext | None = None,
    digests: dict[str, str] | None = None,
) -> tuple[list[Any], list[tuple[EventType, dict[str, Any]]], McpSessions]:
    """和 assembly._mcp_tool_list 同一套：问网关主体能用什么 → 读元数据造工具。"""
    catalog = await gateway_catalog(GATEWAY, ttl_s=0, redis=None)
    access = await subject_access(GATEWAY, subject, ttl_s=0)

    async def fetch() -> str | None:
        return (await subject_access(GATEWAY, subject, ttl_s=0)).key

    sessions = McpSessions(auth=KeyAuth(access.key) if access.key else None, fetch_key=fetch)
    notices: list[tuple[EventType, dict[str, Any]]] = []
    tools = await NativeMcpTools(
        catalog, call_timeout_s=call_timeout_s, max_result_chars=max_result_chars
    ).tools_for(
        tool_names,
        call_ctx or ctx(auth=subject if subject != ALICE else None),
        digests=digests,
        reviews=catalog.reviews(),
        notices=notices,
        access=access,
        sessions=sessions,
    )
    return tools, notices, sessions


async def call(tools: list[Any], name: str, **args: Any) -> Any:
    tool = next(t for t in tools if t.name == name)
    return await tool.ainvoke({"type": "tool_call", "id": "c1", "name": name, "args": args})


def text_of(message: Any) -> str:
    content = message.content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content)
