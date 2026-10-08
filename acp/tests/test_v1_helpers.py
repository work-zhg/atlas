"""atlas_acp.v1 的手写辅助：能力位解读、从原始 update 读控制信号。"""

from __future__ import annotations

from typing import Any

import pytest
from atlas_acp.v1 import AgentCaps, ToolStatus, tool_status, update_kind

# ─────────────────────────── 能力位 ───────────────────────────


def test_capabilities_absent_means_unsupported() -> None:
    assert AgentCaps.from_raw(None) == AgentCaps()
    assert AgentCaps.from_raw({}) == AgentCaps()


def test_session_capability_objects_mean_supported_even_when_empty() -> None:
    """★ resume / close 是对象，出现即支持，{} 也算。"""
    caps = AgentCaps.from_raw(
        {"loadSession": True, "sessionCapabilities": {"resume": {}, "close": {}}}
    )
    assert (caps.load_session, caps.resume, caps.close, caps.list_sessions) == (
        True,
        True,
        True,
        False,
    )


def test_mcp_and_prompt_capabilities() -> None:
    caps = AgentCaps.from_raw(
        {
            "mcpCapabilities": {"http": True, "sse": False},
            "promptCapabilities": {"image": True, "embeddedContext": True},
        }
    )
    assert (caps.mcp_http, caps.mcp_sse, caps.prompt_image, caps.prompt_embedded_context) == (
        True,
        False,
        True,
        True,
    )


def test_malformed_capabilities_are_distrusted() -> None:
    """能力位宁可少信：形状不对就当什么都不支持。"""
    assert AgentCaps.from_raw({"loadSession": "yes please"}) == AgentCaps()


def test_unknown_capabilities_are_ignored() -> None:
    assert AgentCaps.from_raw({"loadSession": True, "_meta": {"x": 1}, "future": {}}).load_session


# ─────────────────────────── update 的控制信号 ───────────────────────────


def _n(update: Any) -> dict[str, Any]:
    return {"sessionId": "s1", "update": update}


def test_update_kind() -> None:
    assert update_kind(_n({"sessionUpdate": "agent_message_chunk"})) == "agent_message_chunk"


@pytest.mark.parametrize("bad", [None, [], "x", {"update": 3}, {"update": {"sessionUpdate": 5}}])
def test_update_kind_tolerates_garbage(bad: Any) -> None:
    assert update_kind(bad) is None


def test_tool_status_reads_id_and_status() -> None:
    status = tool_status(
        _n({"sessionUpdate": "tool_call", "toolCallId": "c1", "status": "in_progress"})
    )
    assert status == ToolStatus("c1", "in_progress") and not status.finished
    done = tool_status(
        _n({"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "failed"})
    )
    assert done is not None and done.finished


def test_tool_status_without_status_field() -> None:
    """v1 的 tool_call_update 只带变化的字段，没有 status 是正常的。"""
    assert tool_status(_n({"sessionUpdate": "tool_call_update", "toolCallId": "c1"})) == ToolStatus(
        "c1", None
    )


@pytest.mark.parametrize(
    "update",
    [
        {"sessionUpdate": "agent_message_chunk", "toolCallId": "c1"},
        {"sessionUpdate": "tool_call"},
        {"sessionUpdate": "tool_call", "toolCallId": ""},
        {"sessionUpdate": "tool_call", "toolCallId": 7},
    ],
)
def test_tool_status_ignores_non_tool_or_malformed(update: dict[str, Any]) -> None:
    assert tool_status(_n(update)) is None


def test_tool_status_does_not_modify_the_input() -> None:
    """★ 读控制信号不能改动要原样转交的内容。"""
    params = _n({"sessionUpdate": "tool_call", "toolCallId": "c1", "status": "pending"})
    snapshot = repr(params)
    tool_status(params)
    update_kind(params)
    assert repr(params) == snapshot
