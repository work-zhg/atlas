"""数据面：认人 → 判权 → 复核 → 改写身份 → 换网关凭据转发 → tools/list 过滤 → 访问日志。

客户端用 FastMCP（和 Atlas、Pod 里的 CLI 同一种），经 ASGITransport 连进程内的网关；
网关再经真实的 HTTP 调后台线程里的 Higress 替身。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from atlas_mcpgateway.db.models import McpAccessLog
from atlas_mcpgateway.db.session import get_sessionmaker
from fake_higress import FakeHigress
from gateway_testkit import (
    ADMIN,
    ALICE,
    BOB,
    FakePermissions,
    approve,
    as_user,
    issue_key,
    mcp_client,
    register,
    weather_and_ticket,
)
from mcp.shared.exceptions import MCPError
from sqlalchemy import select


@pytest.fixture
async def ready(client: httpx.AsyncClient, perms: FakePermissions, higress: FakeHigress) -> str:
    """weather（不要求复核）、ticket（只批准了 create_ticket）都授给了 alice；返回 alice 的 Key。"""
    weather_and_ticket(higress)
    assert (await register(client, "weather", review=False)).status_code == 201
    assert (await register(client, "ticket")).status_code == 201
    await approve(client, "ticket", "create_ticket")
    perms.allow("weather", ALICE)
    perms.allow("ticket", ALICE)
    return await issue_key(client, ALICE)


async def _rpc(
    client: httpx.AsyncClient, service: str, key: str | None, method: str, **params: Any
) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return await client.post(
        f"/mcp/{service}",
        json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params},
        headers=headers,
    )


async def _log() -> list[McpAccessLog]:
    async with get_sessionmaker()() as session:
        return list(await session.scalars(select(McpAccessLog).order_by(McpAccessLog.id)))


async def test_list_and_call_through_gateway(app: Any, ready: str, higress: FakeHigress) -> None:
    async with mcp_client(app, "ticket", ready, **{"X-Uid": BOB, "X-Run-Id": "run-1"}) as c:
        tools = [t.name for t in await c.list_tools()]
        # 待复核的 delete_ticket 被网关从列表里去掉
        assert tools == ["create_ticket"]
        result = await c.call_tool("create_ticket", {"title": "带伞"})
    assert result.content[0].text == f"T-001 带伞 提交人={ALICE} 使用者={BOB}"

    seen = higress.services["ticket"].seen
    call = next(h for m, h in seen if m == "tools/call")
    # 主体 Key 换成了网关凭据；身份由网关写入；审计用的头照转
    assert call["authorization"] == "Bearer gw-test-credential"
    assert call["x-auth-uid"] == ALICE
    assert call["x-uid"] == BOB
    assert call["x-run-id"] == "run-1"
    assert call["x-caller-app"] == "mcp-gateway"

    log = await _log()
    assert {(r.rpc_method, r.outcome) for r in log} >= {
        ("initialize", "ok"),
        ("tools/list", "ok"),
        ("tools/call", "ok"),
    }
    assert all(str(r.subject_uid) == ALICE and r.forwarded for r in log)
    assert next(r for r in log if r.rpc_method == "tools/call").tool == "create_ticket"


async def test_forged_identity_headers_are_dropped(
    app: Any, ready: str, higress: FakeHigress
) -> None:
    async with mcp_client(
        app, "ticket", ready, **{"X-Auth-Uid": ADMIN, "X-Atlas-User": ADMIN}
    ) as c:
        result = await c.call_tool("create_ticket", {"title": "伪造身份"})
    assert f"提交人={ALICE}" in result.content[0].text
    call = next(h for m, h in higress.services["ticket"].seen if m == "tools/call")
    assert "x-atlas-user" not in call


async def test_unreviewed_tool_call_is_rejected_with_reason(app: Any, ready: str) -> None:
    async with mcp_client(app, "ticket", ready) as c:
        with pytest.raises(MCPError) as caught:
            await c.call_tool("delete_ticket", {})
    error = caught.value.error
    assert error.data["reason"] == "tool_pending_review"
    assert error.data["forwarded"] is False
    assert "待复核" in error.message
    call = next(r for r in await _log() if r.rpc_method == "tools/call")
    assert (call.outcome, call.forwarded, call.tool) == (
        "tool_pending_review",
        False,
        "delete_ticket",
    )


async def test_rejections_carry_json_rpc_errors(
    client: httpx.AsyncClient, perms: FakePermissions, ready: str
) -> None:
    missing = await _rpc(client, "weather", None, "tools/list")
    assert missing.status_code == 401
    assert missing.json()["error"]["data"] == {
        "reason": "key_missing",
        "forwarded": False,
        "service": "weather",
    }
    assert missing.json()["id"] == 7

    invalid = await _rpc(client, "weather", "mk-not-a-key", "tools/list")
    assert invalid.json()["error"]["data"]["reason"] == "key_invalid"

    unknown = await _rpc(client, "nope", ready, "tools/list")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["data"]["reason"] == "service_not_found"

    bob_key = await issue_key(client, BOB)
    forbidden = await _rpc(client, "weather", bob_key, "tools/list")
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["data"]["reason"] == "forbidden"

    no_such_tool = await _rpc(client, "weather", ready, "tools/call", name="nope", arguments={})
    assert no_such_tool.json()["error"]["data"]["reason"] == "tool_unknown"


async def test_disabling_service_or_key_takes_effect_immediately(
    app: Any, client: httpx.AsyncClient, ready: str
) -> None:
    async with mcp_client(app, "weather", ready) as c:
        await c.call_tool("get_weather", {"city": "上海"})
        # 会话已经打开（ACP 的 CLI 就是这样）：停用服务照样拦得住
        await client.post("/api/v1/services/weather/disable", headers=as_user(ADMIN))
        with pytest.raises(MCPError) as caught:
            await c.call_tool("get_weather", {"city": "上海"})
        assert caught.value.error.data["reason"] == "service_disabled"
        await client.post("/api/v1/services/weather/enable", headers=as_user(ADMIN))

        key_id = (await client.get("/api/v1/keys", headers=as_user(ADMIN))).json()[0]["id"]
        await client.post(f"/api/v1/keys/{key_id}/disable", headers=as_user(ADMIN))
        with pytest.raises(MCPError) as caught:
            await c.call_tool("get_weather", {"city": "上海"})
        assert caught.value.error.data["reason"] == "key_invalid"


async def test_revoked_grant_takes_effect_within_cache(
    app: Any, client: httpx.AsyncClient, perms: FakePermissions, ready: str
) -> None:
    async with mcp_client(app, "weather", ready) as c:
        await c.call_tool("get_weather", {"city": "上海"})
        perms.grants["weather"].pop(ALICE)
        # 缓存期内仍按旧授权放行（§5.8）
        await c.call_tool("get_weather", {"city": "上海"})
        app.state.usable.forget()
        with pytest.raises(MCPError) as caught:
            await c.call_tool("get_weather", {"city": "上海"})
        assert caught.value.error.data["reason"] == "forbidden"


async def test_user_center_down_after_cache_expired_rejects(
    app: Any, client: httpx.AsyncClient, perms: FakePermissions, ready: str
) -> None:
    app.state.usable.forget()
    perms.down = True
    r = await _rpc(client, "weather", ready, "tools/list")
    assert r.status_code == 503
    assert r.json()["error"]["data"]["reason"] == "authz_unavailable"


async def test_permission_cache_spares_user_center(
    client: httpx.AsyncClient, perms: FakePermissions, ready: str
) -> None:
    app_calls = perms.calls
    for _ in range(5):
        await _rpc(client, "weather", ready, "tools/list")
    assert perms.calls - app_calls <= 1


async def test_definition_drift_seen_in_list_blocks_calls(
    app: Any, client: httpx.AsyncClient, higress: FakeHigress, ready: str
) -> None:
    """上游改了定义、网关还没刷新：tools/list 里指纹对不上 → 写进库、待复核 → 调用也拒（F2）。"""
    tool = higress.services["ticket"].tools["create_ticket"]
    tool["description"] = "在工单系统里建一张工单（新版描述）。"
    async with mcp_client(app, "ticket", ready) as c:
        assert [t.name for t in await c.list_tools()] == []
        with pytest.raises(MCPError) as caught:
            await c.call_tool("create_ticket", {"title": "x"})
    assert caught.value.error.data["reason"] == "tool_pending_review"
    detail = (await client.get("/api/v1/services/ticket", headers=as_user(ADMIN))).json()
    changed = next(t for t in detail["tools"] if t["name"] == "create_ticket")
    assert changed["description"] == "在工单系统里建一张工单（新版描述）。"
    assert changed["status"] == "pending_review"
    assert changed["previous"]["description"] == "在工单系统里建一张工单，返回工单号。"


async def test_sse_responses_are_filtered_too(app: Any, higress: FakeHigress, ready: str) -> None:
    higress.services["ticket"].sse = True
    async with mcp_client(app, "ticket", ready) as c:
        assert [t.name for t in await c.list_tools()] == ["create_ticket"]
        result = await c.call_tool("create_ticket", {"title": "SSE"})
    assert "T-001 SSE" in result.content[0].text


async def test_upstream_rejecting_gateway_credential_is_a_gateway_error(
    client: httpx.AsyncClient, higress: FakeHigress, ready: str
) -> None:
    higress.credential = "rotated-elsewhere"
    try:
        r = await _rpc(client, "weather", ready, "tools/list")
    finally:
        higress.credential = "gw-test-credential"
    assert r.status_code == 502
    assert r.json()["error"]["data"] == {
        "reason": "upstream_rejected",
        "forwarded": True,
        "service": "weather",
    }


async def test_upstream_down_is_reported_as_not_forwarded(
    client: httpx.AsyncClient, higress: FakeHigress, ready: str, monkeypatch: Any
) -> None:
    from atlas_mcpgateway.settings import get_settings

    monkeypatch.setattr(get_settings(), "higress_base_url", "http://127.0.0.1:9")
    r = await _rpc(client, "weather", ready, "tools/list")
    assert r.status_code == 502
    assert r.json()["error"]["data"]["reason"] == "upstream_unavailable"
    assert r.json()["error"]["data"]["forwarded"] is False


async def test_expired_session_is_passed_through(
    app: Any, higress: FakeHigress, ready: str
) -> None:
    """会话失效（上游重启）：网关原样透传上游的 404，客户端据此知道会话没了（§6.3）。"""
    async with mcp_client(app, "weather", ready) as c:
        await c.call_tool("get_weather", {"city": "上海"})
        higress.services["weather"].sessions.clear()
        with pytest.raises(MCPError) as caught:
            await c.call_tool("get_weather", {"city": "上海"})
    assert caught.value.error.data is None
