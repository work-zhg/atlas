"""授权的数据对象（mcp_data_object）、目录事件，以及迁移 g0004 的回填。"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import httpx
from atlas_mcpgateway.access.usercenter import DATA_SERVER, DATA_TOOL, key_access_from
from conftest import TEST_URL
from fake_upstreams import REST_KEY, FakeMcp
from gateway_testkit import ADMIN, ALICE, FakePermissions, apply_user_key, as_user, grant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_mcp_grpc import _create_mcp, _jira


async def _crm(client: httpx.AsyncClient, base: str) -> httpx.Response:
    body = {
        "kind": "rest",
        "name": "crm",
        "connection": {"base_url": f"{base}/rest/api", "spec_url": f"{base}/rest/openapi.json"},
        "credential": {"type": "apikey", "where": "header", "key": "X-Api-Key", "secret": REST_KEY},
        "selected": ["GET /customers/{id}", "GET /customers"],
    }
    return await client.post("/api/v1/services", json=body, headers=as_user(ADMIN))


async def _objects() -> set[tuple[str, str]]:
    engine = create_async_engine(TEST_URL)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT data_code, data_id FROM mcp_data_object"))
            return {(r[0], r[1]) for r in rows}
    finally:
        await engine.dispose()


def test_key_access_groups_tools_by_service() -> None:
    access = key_access_from({"crm"}, {"jira/search", "jira/create", "crm2/x", "broken"})
    assert access.allows("crm", "anything")
    assert access.allows("jira", "search") and not access.allows("jira", "delete")
    assert access.tools_of("jira") == {"search", "create"}
    # 服务名是另一个服务名的前缀时不串（原来按「服务名/」前缀匹配）
    assert not access.touches("crm2/") and access.touches("crm2")
    assert not access.touches("cr")


async def test_tool_objects_follow_grants_and_tool_deletion(
    client: httpx.AsyncClient, perms: FakePermissions, upstream_base: str
) -> None:
    assert (await _crm(client, upstream_base)).status_code == 201
    # 登记服务时授权在用户中心登记 McpServer，并记下
    assert await _objects() == {(DATA_SERVER, "crm")}

    key = await apply_user_key(client, ALICE)
    assert (await grant(client, "crm", key["id"], "tools", ["get_customer"])).status_code == 200
    assert await _objects() == {(DATA_SERVER, "crm"), (DATA_TOOL, "crm/get_customer")}
    assert perms.readers(DATA_TOOL, "crm/get_customer") == {key["subject_uid"]}
    grants = (await client.get("/api/v1/services/crm/grants", headers=as_user(ADMIN))).json()
    assert [(g["key"]["id"], g["scope"], g["tools"]) for g in grants["keys"]] == [
        (key["id"], "tools", ["get_customer"])
    ]

    # 删工具 → 目录发 tools_removed → 授权删掉用户中心里的对象和登记记录
    r = await client.delete("/api/v1/services/crm/tools/get_customer", headers=as_user(ADMIN))
    assert r.status_code == 200, r.text
    assert await _objects() == {(DATA_SERVER, "crm")}
    assert (DATA_TOOL, "crm/get_customer") not in perms.objects


async def test_mcp_refresh_drops_objects_of_removed_tools(
    client: httpx.AsyncClient, perms: FakePermissions, fake_mcp: FakeMcp, upstream_base: str
) -> None:
    svc = _jira(fake_mcp)
    assert (await _create_mcp(client, upstream_base)).status_code == 201
    key = await apply_user_key(client, ALICE)
    assert (await grant(client, "jira", key["id"], "tools", ["broken"])).status_code == 200
    assert (DATA_TOOL, "jira/broken") in await _objects()

    del svc.tools["broken"]
    r = await client.post("/api/v1/services/jira/refresh", headers=as_user(ADMIN))
    assert r.status_code == 200, r.text
    assert await _objects() == {(DATA_SERVER, "jira")}
    assert (DATA_TOOL, "jira/broken") not in perms.objects


async def test_register_rolls_back_when_user_center_fails(
    client: httpx.AsyncClient, perms: FakePermissions, upstream_base: str
) -> None:
    """目录事件在同一个事务里同步分发：用户中心登记失败，服务就登记不上。"""
    perms.down = True
    assert (await _crm(client, upstream_base)).status_code == 503
    perms.down = False
    r = await client.get("/api/v1/services/crm", headers=as_user(ADMIN))
    assert r.status_code == 404
    assert await _objects() == set()
    # 用户中心恢复后照常登记
    assert (await _crm(client, upstream_base)).status_code == 201


async def test_g0004_backfills_data_objects() -> None:
    """g0003 → g0004：每个服务一条 McpServer，uc_registered 的工具各一条 McpTool。"""
    from alembic import command
    from alembic.config import Config
    from atlas_mcpgateway.__main__ import MIGRATIONS_DIR
    from atlas_mcpgateway.settings import get_settings

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    tenant, svc = uuid4(), uuid4()
    await asyncio.to_thread(command.downgrade, cfg, "g0003")
    engine = create_async_engine(TEST_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO mcp_service"
                    " (uuid, created_at, updated_at, tenant_uuid, name, kind)"
                    " VALUES (:u, now(), now(), :t, 'jira', 'mcp')"
                ),
                {"u": svc, "t": tenant},
            )
            for name, registered in (("search", True), ("create", False)):
                await conn.execute(
                    text(
                        "INSERT INTO mcp_tool (uuid, created_at, updated_at, tenant_uuid,"
                        " service_uuid, name, input_schema, digest, uc_registered)"
                        " VALUES (:u, now(), now(), :t, :s, :n, '{}', 'd', :r)"
                    ),
                    {"u": uuid4(), "t": tenant, "s": svc, "n": name, "r": registered},
                )
    finally:
        await engine.dispose()
        await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = create_async_engine(TEST_URL)
    try:
        async with engine.connect() as conn:
            rows: Any = await conn.execute(
                text("SELECT tenant_uuid, data_code, data_id, service_uuid FROM mcp_data_object")
            )
            got = {tuple(r) for r in rows}
            cols = await conn.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'mcp_tool'"
                )
            )
            assert "uc_registered" not in set(cols)
    finally:
        await engine.dispose()
    assert got == {
        (tenant, DATA_SERVER, "jira", svc),
        (tenant, DATA_TOOL, "jira/search", svc),
    }
