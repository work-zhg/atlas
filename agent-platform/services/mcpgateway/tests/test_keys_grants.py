"""API Key（用户 Key、智能体 Key）、给 Key 授权（服务级 / 工具级）、所有者、
给 Atlas 的内部接口、Trace。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from atlas_mcpgateway.perms import DATA_SERVER, DATA_TOOL
from fake_upstreams import REST_KEY
from gateway_testkit import (
    ADMIN,
    ALICE,
    ATLAS,
    BOB,
    CAROL,
    FakePermissions,
    agent_key,
    apply_user_key,
    as_user,
    grant,
    key_id_of,
    mcp_client,
)


async def _crm(client: httpx.AsyncClient, base: str, *, visibility: str = "private") -> None:
    body = {
        "kind": "rest",
        "name": "crm",
        "visibility": visibility,
        "connection": {"base_url": f"{base}/rest/api", "spec_url": f"{base}/rest/openapi.json"},
        "credential": {"type": "apikey", "where": "header", "key": "X-Api-Key", "secret": REST_KEY},
        "selected": ["GET /customers/{id}", "GET /customers", "POST /tickets"],
    }
    r = await client.post("/api/v1/services", json=body, headers=as_user(ADMIN))
    assert r.status_code == 201, r.text


# ───────────────────────────── 用户 Key


async def test_user_key_lifecycle(
    app: Any, client: httpx.AsyncClient, perms: FakePermissions, upstream_base: str
) -> None:
    await _crm(client, upstream_base, visibility="public")
    key = await apply_user_key(client, ALICE)
    assert key["secret"].startswith("mk-")
    assert key["masked"] == f"mk-…{key['secret'][-4:]}"
    # Key 在用户中心有自己的主体（数字员工），负责人是本人
    assert perms.users[key["subject_uid"]]["user_type"] == "digital"
    again = await client.post("/api/v1/keys/me", headers=as_user(ALICE))
    assert again.status_code == 409  # 一人一把

    mine = (await client.get("/api/v1/keys/me", headers=as_user(ALICE))).json()
    assert "secret" not in mine

    reset = (await client.post("/api/v1/keys/me/reset", headers=as_user(ALICE))).json()
    assert reset["secret"] != key["secret"]
    assert reset["subject_uid"] == key["subject_uid"]  # 重置只换原文
    with pytest.raises(Exception, match="API Key 无效或已停用"):  # 旧原文立即失效
        async with mcp_client(app, "crm", key["secret"]) as c:
            await c.list_tools()

    assert (await client.post("/api/v1/keys/me/block", headers=as_user(ALICE))).status_code == 200
    with pytest.raises(Exception, match="API Key 无效或已停用"):
        async with mcp_client(app, "crm", reset["secret"]) as c:
            await c.list_tools()
    assert (await client.post("/api/v1/keys/me/unblock", headers=as_user(ALICE))).status_code == 200
    async with mcp_client(app, "crm", reset["secret"]) as c:
        assert await c.list_tools()

    assert (await client.delete("/api/v1/keys/me", headers=as_user(ALICE))).status_code == 200
    assert perms.users[key["subject_uid"]]["status"] == "disabled"  # 主体停用 = 授权全部失效
    assert (await client.get("/api/v1/keys/me", headers=as_user(ALICE))).json() is None


async def test_admin_cannot_reset_or_delete_user_key(client: httpx.AsyncClient) -> None:
    key = await apply_user_key(client, ALICE)
    assert (
        await client.post(f"/api/v1/keys/{key['id']}/reset", headers=as_user(ADMIN))
    ).status_code == 403
    assert (
        await client.delete(f"/api/v1/keys/{key['id']}", headers=as_user(ADMIN))
    ).status_code == 403
    assert (
        await client.post(f"/api/v1/keys/{key['id']}/block", headers=as_user(ADMIN))
    ).status_code == 200
    assert (await client.get("/api/v1/keys", headers=as_user(ALICE))).status_code == 403


# ───────────────────────────── 智能体 Key（Atlas 的内部接口）


async def test_agent_key_via_internal_api(
    app: Any, client: httpx.AsyncClient, perms: FakePermissions, upstream_base: str
) -> None:
    await _crm(client, upstream_base)
    view = await agent_key(client, "agt_1", name="代码评审助手")
    assert view["status"] == "active" and view["key"].startswith("mk-")
    assert view["servers"] == []  # 新 Key 只能调公开服务（这里没有）
    assert (await agent_key(client, "agt_1"))["key_uid"] == view["key_uid"]  # 幂等
    assert perms.users[view["key_uid"]]["name"].startswith("MCP · ")

    kid = await key_id_of(client, view["key_uid"])
    assert (await grant(client, "crm", kid, "tools", ["get_customer"])).status_code == 200
    got = (
        await client.get(
            "/internal/client-keys",
            params={"client_type": "Agent", "client_code": "agt_1"},
            headers=ATLAS,
        )
    ).json()
    assert got["servers"] == [{"name": "crm", "tools": ["get_customer"]}]

    # 使用者：智能体 Key 认调用方带来的 X-Uid
    async with mcp_client(app, "crm", got["key"], **{"X-Uid": BOB}) as c:
        await c.call_tool_mcp("get_customer", {"id": "C1"})

    r = await client.patch(
        "/internal/client-keys",
        json={"client_type": "Agent", "client_code": "agt_1", "name": "改名了"},
        headers=ATLAS,
    )
    assert r.json()["name"] == "改名了"
    gone = await client.delete(
        "/internal/client-keys",
        params={"client_type": "Agent", "client_code": "agt_1"},
        headers=ATLAS,
    )
    assert gone.json() == {"deleted": True}
    assert perms.users[view["key_uid"]]["status"] == "disabled"


async def test_internal_api_checks_caller_app(client: httpx.AsyncClient) -> None:
    body = {"client_type": "Agent", "client_code": "x", "owner_uid": ALICE}
    assert (await client.post("/internal/client-keys", json=body)).status_code == 403
    assert (
        await client.post(
            "/internal/client-keys", json={**body, "client_type": "Nope"}, headers=ATLAS
        )
    ).status_code == 404
    no_owner = {"client_type": "Agent", "client_code": "y"}
    assert (
        await client.post("/internal/client-keys", json=no_owner, headers=ATLAS)
    ).status_code == 400


async def test_admin_applies_key_for_agent(client: httpx.AsyncClient) -> None:
    body = {
        "client_type": "Agent",
        "client_code": "agt_9",
        "name": "数据分析助手",
        "owner_uid": BOB,
    }
    r = await client.post("/api/v1/client-keys", json=body, headers=as_user(ADMIN))
    assert r.status_code == 201, r.text
    assert "secret" not in r.json()
    assert (
        await client.post("/api/v1/client-keys", json=body, headers=as_user(ADMIN))
    ).status_code == 409


async def test_metadata_marks_disabled_tools(client: httpx.AsyncClient, upstream_base: str) -> None:
    await _crm(client, upstream_base)
    await client.post("/api/v1/services/crm/tools/create_ticket/disable", headers=as_user(ADMIN))
    meta = (await client.get("/internal/metadata")).json()["services"][0]
    assert meta["review_required"] is False
    tools = {t["name"]: t for t in meta["tools"]}
    assert tools["create_ticket"]["status"] == "invalid"
    assert tools["create_ticket"]["issues"] == ["MCP 管理员已停用这个工具"]
    assert tools["get_customer"]["status"] == "ok"


# ───────────────────────────── 授权与所有者


async def test_owner_grants_and_owner_sync(
    client: httpx.AsyncClient, perms: FakePermissions, upstream_base: str
) -> None:
    await _crm(client, upstream_base)
    key = await apply_user_key(client, ALICE)
    # 不是所有者：不能授权
    assert (await grant(client, "crm", key["id"], "all", by=CAROL)).status_code == 403

    r = await client.post(
        "/api/v1/services/crm/owners", json={"user_id": CAROL}, headers=as_user(ADMIN)
    )
    assert r.status_code == 200, r.text
    assert {o["user_id"] for o in r.json()["owners"]} == {ADMIN, CAROL}
    # 所有者能授权；工具级授权登记 McpTool，Owner 和服务一致
    r = await grant(client, "crm", key["id"], "tools", ["get_customer"], by=CAROL)
    assert r.status_code == 200, r.text
    assert perms.owners_of(DATA_TOOL, "crm/get_customer") == {ADMIN, CAROL}
    assert r.json()["keys"][0]["scope"] == "tools"
    assert r.json()["keys"][0]["tools"] == ["get_customer"]

    # 新所有者同步到已登记的工具对象
    await client.post("/api/v1/services/crm/owners", json={"user_id": BOB}, headers=as_user(CAROL))
    assert perms.owners_of(DATA_TOOL, "crm/get_customer") == {ADMIN, CAROL, BOB}

    # 改成服务级：工具级的收回
    r = await grant(client, "crm", key["id"], "all", by=BOB)
    assert perms.readers(DATA_SERVER, "crm") == {key["subject_uid"]}
    assert perms.readers(DATA_TOOL, "crm/get_customer") == set()
    assert r.json()["keys"][0]["scope"] == "all"

    r = await client.delete(f"/api/v1/services/crm/grants/{key['id']}", headers=as_user(BOB))
    assert r.json()["keys"] == []

    r = await client.delete(f"/api/v1/services/crm/owners/{BOB}", headers=as_user(ADMIN))
    assert perms.owners_of(DATA_TOOL, "crm/get_customer") == {ADMIN, CAROL}


async def test_key_side_assignment_only_touches_assignable_services(
    client: httpx.AsyncClient, upstream_base: str
) -> None:
    await _crm(client, upstream_base)
    key = await apply_user_key(client, ALICE)
    body = {
        "grants": [
            {"service": "crm", "scope": "tools", "tools": ["get_customer", "search_customers"]}
        ]
    }
    denied = await client.put(f"/api/v1/keys/{key['id']}/grants", json=body, headers=as_user(BOB))
    assert denied.status_code == 403
    r = await client.put(f"/api/v1/keys/{key['id']}/grants", json=body, headers=as_user(ADMIN))
    assert r.status_code == 200, r.text
    assert r.json() == [
        {
            "service": "crm",
            "display_name": "crm",
            "scope": "tools",
            "tools": ["get_customer", "search_customers"],
        }
    ]
    listed = (await client.get("/api/v1/keys", headers=as_user(ADMIN))).json()
    assert listed[0]["grants"][0]["tools"] == ["get_customer", "search_customers"]
    mine = (await client.get(f"/api/v1/keys/{key['id']}/grants", headers=as_user(ALICE))).json()
    assert mine[0]["service"] == "crm"


async def test_public_service_cannot_be_granted_and_visibility(
    client: httpx.AsyncClient, upstream_base: str
) -> None:
    await _crm(client, upstream_base, visibility="public")
    key = await apply_user_key(client, ALICE)
    assert (await grant(client, "crm", key["id"], "all")).status_code == 400
    # 普通用户看得到公开服务，状态是 public
    listed = (await client.get("/api/v1/services", headers=as_user(ALICE))).json()
    assert [s["my"]["scope"] for s in listed] == ["public"]
    # 改成私有：没授权就看不到
    await client.patch(
        "/api/v1/services/crm", json={"visibility": "private"}, headers=as_user(ADMIN)
    )
    assert (await client.get("/api/v1/services", headers=as_user(ALICE))).json() == []
    assert (await client.get("/api/v1/services/crm", headers=as_user(ALICE))).status_code == 404


async def test_try_call_uses_the_same_checks(client: httpx.AsyncClient, upstream_base: str) -> None:
    await _crm(client, upstream_base)
    key = await apply_user_key(client, ALICE)
    body = {"key_id": key["id"], "arguments": {"id": "C1"}}
    r = await client.post(
        "/api/v1/services/crm/tools/get_customer/call", json=body, headers=as_user(ADMIN)
    )
    assert r.json()["error"]["data"]["reason"] == "forbidden"
    assert len(r.json()["trace_id"]) == 32
    await grant(client, "crm", key["id"], "all")
    r = await client.post(
        "/api/v1/services/crm/tools/get_customer/call", json=body, headers=as_user(ALICE)
    )
    assert r.json()["result"]["structuredContent"]["id"] == "C1"
    # 别人的 Key 不能拿来试调
    other = await client.post(
        "/api/v1/services/crm/tools/get_customer/call", json=body, headers=as_user(BOB)
    )
    assert other.status_code == 403


async def test_user_center_down_denies(
    app: Any, client: httpx.AsyncClient, perms: FakePermissions, upstream_base: str
) -> None:
    await _crm(client, upstream_base)
    key = await apply_user_key(client, ALICE)
    await grant(client, "crm", key["id"], "all")
    perms.down = True
    r = await client.post(
        "/mcp/crm",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Authorization": f"Bearer {key['secret']}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert r.status_code == 503
    assert r.json()["error"]["data"]["reason"] == "authz_unavailable"


# ───────────────────────────── Trace


async def test_spans_carry_gateway_attributes(
    app: Any, client: httpx.AsyncClient, spans: Any, upstream_base: str
) -> None:
    await _crm(client, upstream_base)
    key = await apply_user_key(client, ALICE)
    await grant(client, "crm", key["id"], "tools", ["get_customer"])
    spans.clear()
    async with mcp_client(app, "crm", key["secret"]) as c:
        await c.call_tool_mcp("get_customer", {"id": "C1"})
    finished = spans.get_finished_spans()
    call = next(s for s in finished if s.name == "tools/call get_customer")
    assert call.attributes["mcp.gateway.tool"] == "get_customer"
    assert call.attributes["mcp.gateway.outcome"] == "ok"
    http = next(s for s in finished if s.name == "GET /customers/{id}")
    assert http.parent is not None and http.context.trace_id == call.context.trace_id
    outer = [s for s in finished if s.name == "POST /mcp/crm"]
    assert outer and outer[-1].attributes["mcp.gateway.key_uid"] == key["subject_uid"]
    # 参数、结果不进 Span
    assert all("C1" not in str(dict(s.attributes or {})) for s in finished)

    spans.clear()
    r = await client.post(
        "/mcp/crm",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": "Bearer nope", "Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401
    rejected = next(s for s in spans.get_finished_spans() if s.name == "POST /mcp/crm")
    assert rejected.attributes["error.type"] == "key_invalid"
