"""平台工具：本机 MCP 提供给 CLI、调用经 tool.call 交给 server、在线等、随这一轮结束作罢。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from atlas_bridge.testing.fake_agent import Script
from atlas_bridge.testing.harness import SessionHarness, until
from atlas_bridge.tools.mcp import PlatformMcp
from atlas_host import PlatformTool

TAKEOVER = PlatformTool(
    name="request_takeover",
    description="请用户接管浏览器",
    input_schema={"type": "object", "properties": {"reason": {"type": "string"}}},
)
ENTRY = {"type": "http", "name": "atlas", "url": "http://127.0.0.1:1/mcp", "headers": []}
SLOW = Script(on_prompt="silent")


def harness(make: Any) -> SessionHarness:
    return make(SLOW, platform_entry=lambda: dict(ENTRY))  # type: ignore[no-any-return]


async def test_platform_tools_join_the_cli_mcp_servers_only_when_given(make: Any) -> None:
    h = harness(make)
    await h.open(platform_tools=[TAKEOVER], mcp_servers=[{"type": "http", "name": "kb"}])
    [new] = h.agent_calls("session/new")
    assert [s["name"] for s in new["mcpServers"]] == ["kb", "atlas"]
    assert h.host.platform_tools == [TAKEOVER]
    plain = harness(make)
    await plain.open()
    assert plain.agent_calls("session/new")[0]["mcpServers"] == []


async def test_tool_call_waits_online_and_pauses_the_turn(make: Any) -> None:
    h = harness(make)
    await h.open(platform_tools=[TAKEOVER])
    await h.start()
    call = asyncio.create_task(
        h.host.call_platform_tool("request_takeover", {"reason": "登录"}, "toolu_1")
    )
    await until(lambda: len(h.outbox.of("tool.call")) == 1)
    [msg] = [m for m in h.outbox.messages if m.method == "tool.call"]
    assert msg.wire == {
        "seq": msg.wire["seq"],
        "turnId": "t1",
        "name": "request_takeover",
        "arguments": {"reason": "登录"},
        "callId": "toolu_1",
    }
    # 等人处理：这一轮暂停静默计时（和权限询问一样）
    assert h.outbox.turn_states("t1") == ["running", "awaiting_permission"]
    h.clock.advance(10_000)
    assert not call.done()
    h.host.answer_permission(msg.request_id, {"content": "用户已交还", "isError": False})
    assert await call == {"content": "用户已交还", "isError": False}
    assert h.outbox.turn_states("t1") == ["running", "awaiting_permission", "running"]
    assert h.outbox.of("permission.withdraw") == []


async def test_turn_ending_answers_the_tool_and_withdraws(make: Any) -> None:
    h = harness(make)
    await h.open(platform_tools=[TAKEOVER])
    await h.start()
    call = asyncio.create_task(h.host.call_platform_tool("request_takeover", {}, "toolu_2"))
    await until(lambda: len(h.outbox.of("tool.call")) == 1)
    h.host.cancel_turn("t1")
    result = await call
    assert result["isError"] is True and "这一轮已经结束" in result["content"]
    [withdraw] = h.outbox.of("permission.withdraw")
    assert withdraw["reason"] == "turn_ended"


async def test_server_error_and_no_turn_become_tool_errors(make: Any) -> None:
    h = harness(make)
    await h.open(platform_tools=[TAKEOVER])
    idle = await h.host.call_platform_tool("request_takeover", {}, "toolu_0")
    assert idle["isError"] is True
    await h.start()
    call = asyncio.create_task(h.host.call_platform_tool("request_takeover", {}, "toolu_3"))
    await until(lambda: len(h.outbox.of("tool.call")) == 1)
    [msg] = [m for m in h.outbox.messages if m.method == "tool.call"]
    h.host.answer_permission(msg.request_id, error={"code": -32603, "message": "炸了"})
    result = await call
    assert result["isError"] is True and "炸了" in result["content"]


# ------------------------------------------------------------------ 本机 MCP（HTTP）


async def _post(mcp: PlatformMcp, body: dict[str, Any], *, token: str | None = None) -> bytes:
    entry = mcp.entry()
    auth = token if token is not None else entry["headers"][0]["value"]
    reader, writer = await asyncio.open_connection("127.0.0.1", mcp.port)
    raw = json.dumps(body).encode()
    writer.write(
        b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        + f"Authorization: {auth}\r\nContent-Length: {len(raw)}\r\n\r\n".encode()
        + raw
    )
    await writer.drain()
    data = await reader.read()
    writer.close()
    return data


async def test_mcp_lists_and_calls_tools_over_sse_with_keepalive() -> None:
    gate = asyncio.Event()
    calls: list[tuple[str, dict[str, Any], str]] = []

    async def call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
        calls.append((name, args, call_id))
        await gate.wait()
        return {"content": "用户已交还", "isError": False}

    mcp = PlatformMcp(lambda: [TAKEOVER], call, keepalive_s=0.05)
    await mcp.start()
    try:
        init = await _post(mcp, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert b"200 OK" in init and b'"name": "atlas"' in init
        listed = await _post(mcp, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert b'"request_takeover"' in listed and b'"inputSchema"' in listed
        note = await _post(mcp, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert note.startswith(b"HTTP/1.1 202")
        body = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "request_takeover",
                "arguments": {"reason": "登录"},
                "_meta": {"claudecode/toolUseId": "toolu_9", "progressToken": 7},
            },
        }
        pending = asyncio.create_task(_post(mcp, body))
        await asyncio.sleep(0.2)
        gate.set()
        streamed = await pending
        assert b"text/event-stream" in streamed and b": keepalive" in streamed
        assert b"notifications/progress" in streamed
        last = streamed.rsplit(b"data: ", 1)[1]
        assert json.loads(last)["result"] == {
            "content": [{"type": "text", "text": "用户已交还"}],
            "isError": False,
        }
        assert calls == [("request_takeover", {"reason": "登录"}, "toolu_9")]
        denied = await _post(mcp, {"jsonrpc": "2.0", "id": 4, "method": "ping"}, token="Bearer x")
        assert denied.startswith(b"HTTP/1.1 401")
        unknown = await _post(
            mcp, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "rm"}}
        )
        assert b"-32602" in unknown
    finally:
        await mcp.stop()


def test_cli_gets_unlimited_mcp_tool_timeouts() -> None:
    from atlas_bridge.app import cli_env
    from atlas_bridge.config import BridgeConfig

    config = BridgeConfig(session_id="s", agent_cmd=("x",), agent_env_allow=("PATH",))
    env = cli_env(config, {"PATH": "/bin", "SECRET": "1"})
    assert env == {
        "PATH": "/bin",
        "MCP_TOOL_TIMEOUT": "2147483647",
        "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT": "0",
    }
