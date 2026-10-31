"""MCP 代理（网关作为客户端调上游 MCP Server）与 gRPC 转换（反射 / 描述文件、一元、服务端流）。"""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest
from fake_upstreams import (
    GRPC_TOKEN,
    MCP_TOKEN,
    FakeGrpc,
    FakeMcp,
    FakeMcpService,
    RpcFailure,
    ToolFailure,
)
from gateway_testkit import ADMIN, ALICE, apply_user_key, as_user, grant, mcp_client
from mcp.shared.exceptions import MCPError
from opentelemetry import trace

# ───────────────────────────── MCP 代理


def _jira(fake_mcp: FakeMcp) -> FakeMcpService:
    svc = FakeMcpService()
    svc.tool(
        "search_issues",
        "用 JQL 搜索问题",
        lambda args, h: f"找到 1 个：{args['jql']} 主体={h.get('x-auth-uid')}",
        properties={"jql": {"type": "string"}},
        annotations={"readOnlyHint": True},
    )

    def create(args: dict[str, Any], _h: dict[str, str]) -> str:
        if not args.get("summary"):
            raise ToolFailure("summary 不能为空")
        return "ATLAS-1"

    svc.tool("create_issue", "新建问题", create, properties={"summary": {"type": "string"}})

    def boom(_a: dict[str, Any], _h: dict[str, str]) -> str:
        raise RpcFailure("上游内部错误")

    svc.tool("broken", "总是出错", boom, annotations={"destructiveHint": True})
    fake_mcp.services["jira"] = svc
    return svc


async def _create_mcp(client: httpx.AsyncClient, base: str, **over: Any) -> httpx.Response:
    body = {
        "kind": "mcp",
        "name": "jira",
        "connection": {"url": f"{base}/mcp/jira", "transport": "streamable"},
        "credential": {"type": "bearer", "secret": MCP_TOKEN},
        **over,
    }
    return await client.post("/api/v1/services", json=body, headers=as_user(ADMIN))


async def test_mcp_proxy_registers_all_tools_with_effects(
    client: httpx.AsyncClient, fake_mcp: FakeMcp, upstream_base: str
) -> None:
    _jira(fake_mcp)
    r = await _create_mcp(client, upstream_base)
    assert r.status_code == 201, r.text
    tools = {t["name"]: t for t in r.json()["tools"]}
    assert set(tools) == {"search_issues", "create_issue", "broken"}
    # 副作用取上游注解；没标注的按「有副作用」
    assert tools["search_issues"]["effect"] == "read"
    assert tools["create_issue"]["effect"] == "write"
    assert tools["broken"]["effect"] == "destructive"
    assert all(t["callable"] for t in tools.values())  # 没有复核：拉到就能用


async def test_mcp_proxy_refuses_bad_credential(
    client: httpx.AsyncClient, fake_mcp: FakeMcp, upstream_base: str
) -> None:
    _jira(fake_mcp)
    r = await _create_mcp(client, upstream_base, credential={"type": "bearer", "secret": "wrong"})
    assert r.status_code == 400
    assert r.json()["code"] == "DISCOVERY_FAILED"


async def test_mcp_proxy_call_injects_credential_identity_and_trace(
    app: Any, client: httpx.AsyncClient, fake_mcp: FakeMcp, upstream_base: str
) -> None:
    svc = _jira(fake_mcp)
    assert (await _create_mcp(client, upstream_base)).status_code == 201
    key = await apply_user_key(client, ALICE)
    assert (await grant(client, "jira", key["id"], "all")).status_code == 200
    # 调用方（Atlas 的一个 Run）在自己的 trace 里调：MCP SDK 的客户端把上下文写进 params._meta
    with trace.get_tracer("test").start_as_current_span("atlas run") as root:
        trace_id = format(root.get_span_context().trace_id, "032x")
        async with mcp_client(app, "jira", key["secret"]) as c:
            listed = await c.list_tools()
            assert {t.name for t in listed} == {"search_issues", "create_issue", "broken"}
            before = len(svc.seen)
            ok = await c.call_tool_mcp("search_issues", {"jql": "project = A"})
            assert ok.content[0].text == f"找到 1 个：project = A 主体={key['subject_uid']}"
            failed = await c.call_tool_mcp("create_issue", {})
            assert failed.is_error is True  # 工具执行失败：原样交给调用方
            with pytest.raises(MCPError) as upstream:
                await c.call_tool_mcp("broken", {})
    # tools/list 不转发给上游；一次调用 = initialize + initialized + tools/call
    methods = [m for m, _h, _p in svc.seen[before:]]
    assert "tools/list" not in methods
    assert methods[:3] == ["initialize", "notifications/initialized", "tools/call"]
    assert upstream.value.error.code == -32051
    assert upstream.value.error.data["reason"] == "upstream_error"
    assert upstream.value.error.data["forwarded"] is True

    headers, params = svc.calls()[0]
    assert headers["authorization"] == f"Bearer {MCP_TOKEN}"  # 网关凭据，不是调用方的 Key
    assert headers["x-auth-uid"] == key["subject_uid"]
    # 追踪上下文：上游收到的 HTTP 头和 params._meta 都在调用方的那条 trace 里
    assert headers["traceparent"].split("-")[1] == trace_id
    assert params["_meta"]["traceparent"].split("-")[1] == trace_id


async def test_mcp_refresh_picks_up_new_and_removed_tools(
    app: Any, client: httpx.AsyncClient, fake_mcp: FakeMcp, upstream_base: str
) -> None:
    svc = _jira(fake_mcp)
    assert (await _create_mcp(client, upstream_base)).status_code == 201
    key = await apply_user_key(client, ALICE)
    assert (await grant(client, "jira", key["id"], "all")).status_code == 200

    svc.tool("add_comment", "评论", lambda a, _h: "ok", properties={"body": {"type": "string"}})
    del svc.tools["broken"]
    r = await client.post("/api/v1/services/jira/refresh", headers=as_user(ADMIN))
    assert r.status_code == 200, r.text
    body = r.json()
    assert {t["name"] for t in body["tools"]} == {"search_issues", "create_issue", "add_comment"}
    assert body["removed_tools"] == ["broken"]
    # 服务级授权：新工具自动可用
    async with mcp_client(app, "jira", key["secret"]) as c:
        assert "add_comment" in {t.name for t in await c.list_tools()}


async def test_mcp_tools_cannot_be_edited(
    client: httpx.AsyncClient, fake_mcp: FakeMcp, upstream_base: str
) -> None:
    _jira(fake_mcp)
    assert (await _create_mcp(client, upstream_base)).status_code == 201
    r = await client.patch(
        "/api/v1/services/jira/tools/create_issue", json={"effect": "read"}, headers=as_user(ADMIN)
    )
    assert r.status_code == 400


# ───────────────────────────── gRPC


async def _create_grpc(client: httpx.AsyncClient, address: str, **over: Any) -> httpx.Response:
    body = {
        "kind": "grpc",
        "name": "order",
        "visibility": "public",
        "connection": {"address": address, "tls": "plaintext", "discovery": "reflection"},
        "credential": {"type": "metadata", "key": "Authorization", "secret": GRPC_TOKEN},
        "selected": [
            "demo.v1.OrderService/GetOrder",
            "demo.v1.OrderService/ListItems",
            "demo.v1.OrderService/DeleteOrder",
        ],
        **over,
    }
    return await client.post("/api/v1/services", json=body, headers=as_user(ADMIN))


async def test_grpc_reflection_lists_methods(
    client: httpx.AsyncClient, fake_grpc: FakeGrpc
) -> None:
    body = {
        "kind": "grpc",
        "connection": {"address": fake_grpc.address, "tls": "plaintext", "discovery": "reflection"},
        "credential": {"type": "metadata", "key": "authorization", "secret": GRPC_TOKEN},
    }
    r = await client.post("/api/v1/discover", json=body, headers=as_user(ADMIN))
    assert r.status_code == 200, r.text
    found = r.json()
    assert found["services"] == ["demo.v1.OrderService"]  # 反射服务本身不列
    tools = {t["key"]: t for t in found["tools"]}
    assert tools["demo.v1.OrderService/GetOrder"]["name"] == "get_order"
    assert tools["demo.v1.OrderService/GetOrder"]["effect"] == "read"
    assert tools["demo.v1.OrderService/DeleteOrder"]["effect"] == "destructive"
    assert tools["demo.v1.OrderService/ListItems"]["stream"] == "server"
    assert tools["demo.v1.OrderService/Chat"]["unsupported"] == "双向流"
    schema = tools["demo.v1.OrderService/GetOrder"]["input_schema"]
    assert schema == {"type": "object", "properties": {"order_id": {"type": "string"}}}


async def test_grpc_unary_stream_and_errors(
    app: Any, client: httpx.AsyncClient, fake_grpc: FakeGrpc
) -> None:
    r = await _create_grpc(client, fake_grpc.address)
    assert r.status_code == 201, r.text
    assert r.json()["connection"]["services"] == ["demo.v1.OrderService"]
    key = await apply_user_key(client, ALICE)
    async with mcp_client(app, "order", key["secret"]) as c:
        order = await c.call_tool_mcp("get_order", {"order_id": "O-1"})
        assert order.structured_content == {
            "order_id": "O-1",
            "status": "PAID",
            "amount": 1299,
            "tags": ["vip"],
        }
        items = await c.call_tool_mcp("list_items", {"order_id": "O-1"})
        assert [m["sku"] for m in items.structured_content["messages"]] == [
            "SKU-0",
            "SKU-1",
            "SKU-2",
        ]
        missing = await c.call_tool_mcp("delete_order", {"order_id": "O-9"})
        assert missing.is_error is True
        assert missing.content[0].text == "gRPC NOT_FOUND: order O-9 not found"
        with pytest.raises(MCPError) as bad:
            await c.call_tool_mcp("get_order", {"order_id": 5})
    assert bad.value.error.code == -32602

    name, metadata, request = fake_grpc.seen[0]
    assert name == "GetOrder" and request == {"order_id": "O-1"}
    assert metadata["authorization"] == GRPC_TOKEN
    assert metadata["x-auth-uid"] == key["subject_uid"]
    assert metadata["x-uid"] == ALICE


async def test_grpc_descriptor_upload(
    app: Any, client: httpx.AsyncClient, fake_grpc: FakeGrpc
) -> None:
    descriptor = base64.b64encode(fake_grpc.descriptor_set()).decode()
    r = await _create_grpc(
        client,
        fake_grpc.address,
        name="order2",
        connection={"address": fake_grpc.address, "tls": "plaintext", "discovery": "descriptor"},
        descriptor=descriptor,
        selected=["demo.v1.OrderService/GetOrder"],
    )
    assert r.status_code == 201, r.text
    key = await apply_user_key(client, ALICE)
    async with mcp_client(app, "order2", key["secret"]) as c:
        assert [t.name for t in await c.list_tools()] == ["get_order"]

    no_file = await _create_grpc(
        client,
        fake_grpc.address,
        name="order3",
        connection={"address": fake_grpc.address, "tls": "plaintext", "discovery": "descriptor"},
    )
    assert no_file.status_code == 400


async def test_grpc_upstream_down_is_unavailable(
    app: Any, client: httpx.AsyncClient, fake_grpc: FakeGrpc
) -> None:
    descriptor = base64.b64encode(fake_grpc.descriptor_set()).decode()
    r = await _create_grpc(
        client,
        "127.0.0.1:1",
        connection={"address": "127.0.0.1:1", "tls": "plaintext", "discovery": "descriptor"},
        descriptor=descriptor,
        selected=["demo.v1.OrderService/GetOrder"],
    )
    assert r.status_code == 201, r.text
    key = await apply_user_key(client, ALICE)
    async with mcp_client(app, "order", key["secret"]) as c:
        with pytest.raises(MCPError) as down:
            await c.call_tool_mcp("get_order", {"order_id": "O-1"})
    assert down.value.error.code == -32050
    assert down.value.error.data["reason"] == "upstream_unavailable"
