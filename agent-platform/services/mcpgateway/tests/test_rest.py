"""REST 转换：解析 OpenAPI → 挑操作登记 → 按授权调用
（拼路径、查询参数、请求体，注入凭据和身份）。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from atlas_mcpgateway.perms import DATA_SERVER, DATA_TOOL
from fake_upstreams import REST_KEY, FakeRest
from gateway_testkit import (
    ADMIN,
    ALICE,
    BOB,
    FakePermissions,
    apply_user_key,
    as_user,
    grant,
    mcp_client,
)
from mcp.shared.exceptions import MCPError


def _rest(base: str, **over: Any) -> dict[str, Any]:
    return {
        "kind": "rest",
        "connection": {"base_url": f"{base}/rest/api", "spec_url": f"{base}/rest/openapi.json"},
        "credential": {"type": "apikey", "where": "header", "key": "X-Api-Key", "secret": REST_KEY},
        **over,
    }


async def _create(client: httpx.AsyncClient, base: str, **over: Any) -> httpx.Response:
    body = _rest(
        base,
        name="crm",
        display_name="客户管理",
        selected=["GET /customers/{id}", "GET /customers", "POST /tickets", "GET /fail"],
        **over,
    )
    return await client.post("/api/v1/services", json=body, headers=as_user(ADMIN))


async def test_discover_lists_operations_and_marks_unsupported(
    client: httpx.AsyncClient, upstream_base: str
) -> None:
    r = await client.post("/api/v1/discover", json=_rest(upstream_base), headers=as_user(ADMIN))
    assert r.status_code == 200, r.text
    tools = {t["key"]: t for t in r.json()["tools"]}
    assert tools["GET /customers/{id}"]["name"] == "get_customer"
    assert tools["GET /customers/{id}"]["effect"] == "read"
    assert tools["DELETE /customers/{id}"]["effect"] == "destructive"
    # 请求体的字段（$ref 解开）成了参数，required 带过来
    ticket = tools["POST /tickets"]["input_schema"]
    assert set(ticket["properties"]) == {"customer_id", "title", "priority"}
    assert set(ticket["required"]) == {"customer_id", "title"}
    assert "multipart/form-data" in tools["POST /attachments"]["unsupported"]


async def test_discover_needs_manager(client: httpx.AsyncClient, upstream_base: str) -> None:
    r = await client.post("/api/v1/discover", json=_rest(upstream_base), headers=as_user(ALICE))
    assert r.status_code == 403


async def test_create_registers_owner_and_tools(
    client: httpx.AsyncClient, perms: FakePermissions, upstream_base: str
) -> None:
    r = await _create(client, upstream_base)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "rest" and body["visibility"] == "private"
    # 凭据不回显
    assert body["credential"] == {
        "type": "apikey",
        "key": "X-Api-Key",
        "where": "header",
        "user": None,
        "token_url": None,
        "scope": None,
        "cert": None,
        "hint": f"••••{REST_KEY[-4:]}",
    }
    assert {t["name"] for t in body["tools"]} == {
        "get_customer",
        "search_customers",
        "create_ticket",
        "always_fail",
    }
    get = next(t for t in body["tools"] if t["name"] == "get_customer")
    assert get["mapping"]["path"] == "/customers/{id}"
    assert get["annotations"] == {"readOnlyHint": True}
    assert perms.owners_of(DATA_SERVER, "crm") == {ADMIN}


async def test_create_refuses_unsupported_operation(
    client: httpx.AsyncClient, upstream_base: str
) -> None:
    body = _rest(upstream_base, name="crm", selected=["POST /attachments"])
    r = await client.post("/api/v1/services", json=body, headers=as_user(ADMIN))
    assert r.status_code == 400
    assert "不支持转换" in r.json()["detail"]


async def test_private_service_needs_grant_and_tool_level_grant_filters(
    app: Any,
    client: httpx.AsyncClient,
    perms: FakePermissions,
    fake_rest: FakeRest,
    upstream_base: str,
) -> None:
    assert (await _create(client, upstream_base)).status_code == 201
    key = await apply_user_key(client, ALICE)

    # 没有授权：一连上就被拒
    with pytest.raises(Exception) as denied:
        async with mcp_client(app, "crm", key["secret"]) as c:
            await c.list_tools()
    assert "没有被授权" in str(denied.value)

    # 工具级授权：只看得到、只调得了这两个
    r = await grant(client, "crm", key["id"], "tools", ["get_customer", "search_customers"])
    assert r.status_code == 200, r.text
    assert perms.readers(DATA_TOOL, "crm/get_customer") == {key["subject_uid"]}
    # McpTool 第一次授权时登记，Owner = 服务此刻的所有者
    assert perms.owners_of(DATA_TOOL, "crm/get_customer") == {ADMIN}

    audit = {"X-Uid": BOB, "X-Run-Id": "run-1", "X-Thread-Id": "thread-1", "X-Other": "x"}
    async with mcp_client(app, "crm", key["secret"], **audit) as c:
        names = {t.name for t in await c.list_tools()}
        assert names == {"get_customer", "search_customers"}
        result = await c.call_tool_mcp("get_customer", {"id": "C 1"})
        assert result.is_error is False
        assert result.structured_content == {"id": "C 1", "name": "云帆科技", "level": "B"}
        with pytest.raises(MCPError) as forbidden:
            await c.call_tool_mcp("create_ticket", {"customer_id": "C1", "title": "x"})
    assert forbidden.value.error.code == -32044
    assert forbidden.value.error.data["reason"] == "forbidden"
    assert forbidden.value.error.data["forwarded"] is False

    method, path, _query, headers, _body = fake_rest.seen[-1]
    assert (method, path) == ("GET", "/customers/C 1")
    assert headers["x-api-key"] == REST_KEY  # 上游凭据由网关注入
    assert headers["x-auth-uid"] == key["subject_uid"]  # X-Auth-Uid = Key 的主体
    assert headers["x-uid"] == ALICE  # 用户 Key：使用者是持有人，不认调用方自带的 X-Uid
    assert (headers["x-run-id"], headers["x-thread-id"]) == ("run-1", "thread-1")  # 审计头照转
    assert "authorization" not in headers  # 调用方的 Key 不转发
    assert "x-other" not in headers  # 别的请求头不转发


async def test_call_maps_query_body_pick_and_errors(
    app: Any, client: httpx.AsyncClient, fake_rest: FakeRest, upstream_base: str
) -> None:
    assert (await _create(client, upstream_base)).status_code == 201
    key = await apply_user_key(client, ALICE)
    assert (await grant(client, "crm", key["id"], "all")).status_code == 200
    # 只取 $.items
    r = await client.patch(
        "/api/v1/services/crm/tools/search_customers",
        json={"mapping": {"pick": "$.items"}},
        headers=as_user(ADMIN),
    )
    assert r.status_code == 200, r.text

    async with mcp_client(app, "crm", key["secret"]) as c:
        found = await c.call_tool_mcp("search_customers", {"q": "云帆", "limit": 5})
        assert found.content[0].text == '[{"id": "C1", "name": "云帆"}]'
        assert found.structured_content is None  # 取出来的是数组：没有结构化结果
        _m, _p, query, _h, _b = fake_rest.seen[-1]
        assert query == {"q": "云帆", "limit": "5"}

        ticket = await c.call_tool_mcp("create_ticket", {"customer_id": "C1", "title": "发票"})
        assert ticket.structured_content == {
            "ticket_id": "T-1",
            "customer_id": "C1",
            "title": "发票",
        }
        assert fake_rest.seen[-1][4] == {"customer_id": "C1", "title": "发票"}

        failed = await c.call_tool_mcp("always_fail", {})
        assert failed.is_error is True
        assert failed.content[0].text.startswith("HTTP 500")

        with pytest.raises(MCPError) as bad:
            await c.call_tool_mcp("create_ticket", {"title": "缺客户"})
    assert bad.value.error.code == -32602
    assert "customer_id" in bad.value.error.message


async def test_manual_tool_with_fixed_params(
    app: Any, client: httpx.AsyncClient, fake_rest: FakeRest, upstream_base: str
) -> None:
    body = _rest(upstream_base, name="crm2", visibility="public")
    body["connection"]["spec_url"] = ""
    r = await client.post("/api/v1/services", json=body, headers=as_user(ADMIN))
    assert r.status_code == 201, r.text
    assert r.json()["tools"] == []
    tool = {
        "name": "top_customers",
        "description": "A 级客户",
        "effect": "read",
        "mapping": {
            "method": "GET",
            "path": "/customers",
            "params": [
                {"name": "q", "in": "query", "type": "string", "fixed": "A 级"},
                {"name": "limit", "in": "query", "type": "integer"},
            ],
        },
    }
    r = await client.post("/api/v1/services/crm2/tools", json=tool, headers=as_user(ADMIN))
    assert r.status_code == 201, r.text
    # 固定值不进参数定义
    assert set(r.json()["input_schema"]["properties"]) == {"limit"}

    bad = {**tool, "name": "bad", "mapping": {"method": "GET", "path": "/customers/{id}"}}
    r = await client.post("/api/v1/services/crm2/tools", json=bad, headers=as_user(ADMIN))
    assert r.status_code == 400
    assert "{id}" in r.json()["detail"]

    key = await apply_user_key(client, BOB)  # 公开服务：不用授权
    async with mcp_client(app, "crm2", key["secret"]) as c:
        await c.call_tool_mcp("top_customers", {"limit": 3})
    assert fake_rest.seen[-1][2] == {"q": "A 级", "limit": "3"}


async def test_disabled_tool_and_bad_key(
    app: Any, client: httpx.AsyncClient, upstream_base: str
) -> None:
    assert (await _create(client, upstream_base, visibility="public")).status_code == 201
    key = await apply_user_key(client, ALICE)
    r = await client.post("/api/v1/services/crm/tools/always_fail/disable", headers=as_user(ADMIN))
    assert r.status_code == 200 and r.json()["callable"] is False
    async with mcp_client(app, "crm", key["secret"]) as c:
        assert "always_fail" not in {t.name for t in await c.list_tools()}
        with pytest.raises(MCPError) as unknown:
            await c.call_tool_mcp("always_fail", {})
    assert unknown.value.error.data["reason"] == "tool_unknown"

    r = await client.post(
        "/mcp/crm",
        json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
        headers={
            "Authorization": "Bearer mk-nope",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert r.status_code == 401
    assert r.json() == {
        "jsonrpc": "2.0",
        "id": 9,
        "error": {
            "code": -32043,
            "message": "API Key 无效或已停用",
            "data": {"reason": "key_invalid", "forwarded": False, "service": "crm"},
        },
    }
    assert (await client.get("/mcp/crm")).status_code == 405


async def test_add_service_from_spec_file(
    app: Any,
    client: httpx.AsyncClient,
    perms: FakePermissions,
    fake_rest: FakeRest,
    upstream_base: str,
) -> None:
    """部署脚本的登记（service add --file）：手工定义的 REST 工具一起建好；已存在就跳过。"""
    from atlas_mcpgateway.__main__ import add_service
    from atlas_mcpgateway.db.session import get_sessionmaker
    from atlas_mcpgateway.registry import Registry
    from atlas_mcpgateway.settings import get_settings

    spec = {
        **_rest(upstream_base, name="search", display_name="搜索", visibility="private"),
        "pass_identity": False,
        "tools": [
            {
                "name": "find_customer",
                "description": "按关键词找客户",
                "mapping": {
                    "method": "GET",
                    "path": "/customers",
                    "params": [
                        {
                            "name": "query",
                            "in": "query",
                            "type": "string",
                            "required": True,
                            "field": "q",
                        },
                        {"name": "limit", "in": "query", "type": "string", "fixed": "5"},
                    ],
                    "pick": "$.items",
                },
            }
        ],
    }
    spec["connection"]["spec_url"] = ""
    for expected in (True, False):
        async with get_sessionmaker()() as session:
            registry = Registry(session, get_settings(), perms, app.state.upstreams)
            row = await add_service(registry, spec, owner=ADMIN)
            await session.commit()
        assert (row is not None) is expected
    assert perms.owners_of(DATA_SERVER, "search") == {ADMIN}

    key = await apply_user_key(client, BOB)
    assert (await grant(client, "search", key["id"], scope="all")).status_code == 200
    async with mcp_client(app, "search", key["secret"]) as c:
        assert [t.name for t in await c.list_tools()] == ["find_customer"]
        await c.call_tool_mcp("find_customer", {"query": "云帆"})
    _method, path, query, headers, _body = fake_rest.seen[-1]
    assert (path, query) == ("/customers", {"q": "云帆", "limit": "5"})
    assert "x-auth-uid" not in headers  # 不传调用者身份
