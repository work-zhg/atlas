"""AcpClient 对进程内 FakeAgent（代码设计 §7.3）。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from atlas_bridge.agent.client import AcpClient
from atlas_bridge.errors import AcpRequestTimeout, AgentProtocolError, AgentRequestFailed
from atlas_bridge.testing.fake_agent import FakeAgent, Script
from atlas_jsonrpc.memory import memory_pair


class Harness:
    def __init__(self, script: Script, permission_answer: dict[str, Any] | None = None) -> None:
        self.updates: list[dict[str, Any]] = []
        self.permission_requests: list[dict[str, Any]] = []
        self.answer = permission_answer or {"outcome": {"outcome": "selected", "optionId": "allow"}}
        client_side, agent_side = memory_pair()
        self.agent = FakeAgent(script)
        self.client = AcpClient(
            client_side, on_update=self._on_update, on_permission=self._on_permission
        )
        self._channels = (client_side, agent_side)
        self._tasks = [
            asyncio.create_task(self.agent.serve(agent_side)),
            asyncio.create_task(self.client.run()),
        ]

    async def _on_update(self, params: dict[str, Any]) -> None:
        self.updates.append(params)

    async def _on_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        self.permission_requests.append(params)
        return self.answer

    async def close(self) -> None:
        self._channels[0].close()
        await asyncio.wait_for(asyncio.gather(*self._tasks, return_exceptions=True), 2)

    def kinds(self) -> list[str]:
        return [u["update"]["sessionUpdate"] for u in self.updates]


@pytest.fixture
async def h(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    harness = Harness(getattr(request, "param", Script()))
    yield harness
    await harness.close()


async def test_initialize_negotiates_caps(h: Harness) -> None:
    negotiated = await h.client.initialize(timeout=1)
    assert negotiated.protocol_version == 1
    assert negotiated.caps.load_session and negotiated.caps.resume and negotiated.caps.mcp_http
    assert negotiated.agent_info == {"name": "fake-agent", "version": "0.0.0"}
    assert negotiated.agent_capabilities["loadSession"] is True  # 原样保留，上报给 server


async def test_client_declares_no_fs_or_terminal(h: Harness) -> None:
    """★ bridge 不声明 fs / terminal（Bridge 设计 §3.7）。"""
    await h.client.initialize(timeout=1)
    caps = h.agent.received[0][1]["clientCapabilities"]
    assert caps["fs"] == {"readTextFile": False, "writeTextFile": False}
    assert caps["terminal"] is False


@pytest.mark.parametrize("h", [Script(protocol_version=2)], indirect=True)
async def test_unsupported_protocol_version_is_rejected(h: Harness) -> None:
    with pytest.raises(AgentProtocolError, match="版本 2"):
        await h.client.initialize(timeout=1)


async def test_a_full_turn(h: Harness) -> None:
    await h.client.initialize(timeout=1)
    sid = await h.client.new_session("/workspace", [], timeout=1)
    future = await h.client.start_prompt(sid, [{"type": "text", "text": "hi"}], trace="00-t-s-01")
    assert (await asyncio.wait_for(future, 2)) == {"stopReason": "end_turn"}
    assert h.kinds() == ["agent_message_chunk", "agent_message_chunk"]
    prompt_params = next(p for m, p in h.agent.received if m == "session/prompt")
    assert prompt_params["_meta"] == {"traceparent": "00-t-s-01"}  # §8.4


@pytest.mark.parametrize("h", [Script(ask_permission=True)], indirect=True)
async def test_permission_request_goes_to_callback_and_answer_goes_back(h: Harness) -> None:
    await h.client.initialize(timeout=1)
    sid = await h.client.new_session("/workspace", [], timeout=1)
    future = await h.client.start_prompt(sid, [{"type": "text", "text": "write"}])
    await asyncio.wait_for(future, 2)
    assert len(h.permission_requests) == 1
    assert h.permission_requests[0]["options"][0]["kind"] == "allow_once"
    assert h.kinds()[-1] == "tool_call_update"
    assert h.updates[-1]["update"]["status"] == "completed"  # 批准了，工具完成


@pytest.mark.parametrize("h", [Script(on_prompt="silent")], indirect=True)
async def test_cancel_is_confirmed_with_cancelled(h: Harness) -> None:
    await h.client.initialize(timeout=1)
    sid = await h.client.new_session("/workspace", [], timeout=1)
    future = await h.client.start_prompt(sid, [{"type": "text", "text": "x"}])
    await asyncio.sleep(0.05)
    assert not future.done()
    await h.client.cancel(sid)
    assert (await asyncio.wait_for(future, 2)) == {"stopReason": "cancelled"}


@pytest.mark.parametrize("h", [Script(on_prompt="silent", on_cancel="error")], indirect=True)
async def test_prompt_error_fails_the_future(h: Harness) -> None:
    await h.client.initialize(timeout=1)
    sid = await h.client.new_session("/workspace", [], timeout=1)
    future = await h.client.start_prompt(sid, [{"type": "text", "text": "x"}])
    await asyncio.sleep(0.05)  # 等 fake agent 进入这一轮，否则 cancel 会先于 prompt 到达
    await h.client.cancel(sid)
    with pytest.raises(Exception, match="aborted"):
        await asyncio.wait_for(future, 2)


async def test_load_replays_history_before_responding(h: Harness) -> None:
    await h.client.initialize(timeout=1)
    sid = await h.client.new_session("/workspace", [], timeout=1)
    await asyncio.wait_for(await h.client.start_prompt(sid, [{"type": "text", "text": "q1"}]), 2)
    h.updates.clear()
    await h.client.load_session(sid, "/workspace", [], timeout=1)
    # load 返回时，重放的历史已经全部送达
    assert h.kinds() == ["user_message_chunk", "agent_message_chunk"]


async def test_agent_error_becomes_agent_request_failed(h: Harness) -> None:
    await h.client.initialize(timeout=1)
    with pytest.raises(AgentRequestFailed) as info:
        await h.client.resume_session("no-such-session", "/workspace", [], timeout=1)
    assert info.value.acp["code"] == -32002


@pytest.mark.parametrize("h", [Script(on_prompt="silent", on_cancel="ignore")], indirect=True)
async def test_request_timeout_becomes_acp_request_timeout(h: Harness) -> None:
    """★ 超时的上层含义：agent 状态未知，应当重启（Bridge 设计 §6.5）。"""
    await h.client.initialize(timeout=1)
    sid = await h.client.new_session("/workspace", [], timeout=1)
    await h.client.start_prompt(sid, [{"type": "text", "text": "x"}])
    await h.client.cancel(sid)
    # 用极短超时，保证响应一定来不及
    with pytest.raises(AcpRequestTimeout) as info:
        await h.client.close_session(sid, timeout=0.000001)
    assert info.value.method == "session/close"


async def test_undeclared_methods_from_agent_get_method_not_found() -> None:
    """agent 若仍发来 fs/*（没有声明能力），回 -32601。"""
    from atlas_jsonrpc import RpcEndpoint

    client_side, agent_side = memory_pair()

    async def noop(*_: Any) -> Any:
        return {}

    client = AcpClient(client_side, on_update=noop, on_permission=noop)
    agent = RpcEndpoint(agent_side, name="agent")
    tasks = [asyncio.create_task(client.run()), asyncio.create_task(agent.run())]
    with pytest.raises(Exception, match="-32601"):
        await agent.request("fs/read_text_file", {"path": "/etc/passwd"}, timeout=1)
    client_side.close()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
