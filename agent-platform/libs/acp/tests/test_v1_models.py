"""契约测试：生成的 ACP v1 模型能解析规范文档中的典型消息（代码设计 §3 · §12.3）。

示例取自 ACP 官方文档（schema-v1.23.0 标签下的 docs/protocol/v1/*.mdx）。
"""

from __future__ import annotations

from typing import Any

import pytest
from atlas_acp.v1 import _generated as g
from atlas_acp.v1 import methods

# ─────────────────────────── 初始化 ───────────────────────────


def test_initialize_request_and_response() -> None:
    req = g.InitializeRequest.model_validate(
        {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            "clientInfo": {"name": "atlas-bridge", "version": "2.0.0"},
        }
    )
    assert req.protocol_version == 1
    resp = g.InitializeResponse.model_validate(
        {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {"image": True, "embeddedContext": True},
                "mcpCapabilities": {"http": True, "sse": False},
                "sessionCapabilities": {"resume": {}, "close": {}},
            },
            "agentInfo": {"name": "claude-agent-acp", "version": "1.0.0"},
            "authMethods": [],
        }
    )
    caps = resp.agent_capabilities
    assert caps is not None and caps.load_session is True
    assert caps.mcp_capabilities is not None and caps.mcp_capabilities.http is True


# ─────────────────────────── 会话 ───────────────────────────


def test_new_session_with_http_and_stdio_mcp_servers() -> None:
    req = g.NewSessionRequest.model_validate(
        {
            "cwd": "/workspace",
            "mcpServers": [
                {"type": "http", "name": "serpapi", "url": "http://gw/mcp", "headers": []},
                {"name": "fs", "command": "/usr/bin/mcp-fs", "args": [], "env": []},
            ],
        }
    )
    http, stdio = req.mcp_servers
    assert isinstance(http, g.McpServerHttp) and http.url == "http://gw/mcp"
    assert isinstance(stdio, g.McpServerStdio) and stdio.command == "/usr/bin/mcp-fs"


def test_load_session_requires_mcp_servers() -> None:
    """★ v1 的 load 必须带 mcpServers（可以为空数组），缺了 agent 会回 -32602。"""
    with pytest.raises(ValueError):
        g.LoadSessionRequest.model_validate({"sessionId": "s1", "cwd": "/workspace"})
    g.LoadSessionRequest.model_validate({"sessionId": "s1", "cwd": "/workspace", "mcpServers": []})


# ─────────────────────────── 一轮 prompt ───────────────────────────


def test_prompt_request_and_response() -> None:
    req = g.PromptRequest.model_validate(
        {"sessionId": "s1", "prompt": [{"type": "text", "text": "hello"}]}
    )
    assert isinstance(req.prompt[0], g.TextContent) and req.prompt[0].text == "hello"
    resp = g.PromptResponse.model_validate({"stopReason": "end_turn"})
    assert resp.stop_reason == g.StopReason.end_turn


def _update(params: dict[str, Any]) -> Any:
    return g.SessionNotification.model_validate(params).update


@pytest.mark.parametrize(
    ("update", "base"),
    [
        (
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}},
            g.ContentChunk,
        ),
        (
            {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "…"}},
            g.ContentChunk,
        ),
        (
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "call_001",
                "title": "Reading configuration file",
                "kind": "read",
                "status": "pending",
            },
            g.ToolCall,
        ),
        (
            {"sessionUpdate": "tool_call_update", "toolCallId": "call_001", "status": "completed"},
            g.ToolCallUpdate,
        ),
        (
            {
                "sessionUpdate": "plan",
                "entries": [{"content": "Check", "priority": "high", "status": "pending"}],
            },
            g.Plan,
        ),
    ],
)
def test_session_update_variants(update: dict, base: type) -> None:
    """联合类型解析到正确的变体：每个变体都继承规范里有名字的类型，并带着判别值。"""
    parsed = _update({"sessionId": "s1", "update": update})
    assert isinstance(parsed, base)
    assert parsed.session_update == update["sessionUpdate"]


def test_tool_call_with_diff_content() -> None:
    parsed = _update(
        {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "c2",
                "title": "Write index.html",
                "kind": "edit",
                "status": "in_progress",
                "content": [{"type": "diff", "path": "/workspace/index.html", "newText": "<html>"}],
            },
        }
    )
    assert parsed.content is not None and len(parsed.content) == 1


# ─────────────────────────── 权限 ───────────────────────────


def test_request_permission_and_outcomes() -> None:
    req = g.RequestPermissionRequest.model_validate(
        {
            "sessionId": "s1",
            "toolCall": {"toolCallId": "call_001"},
            "options": [
                {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
            ],
        }
    )
    assert [o.kind for o in req.options] == [
        g.PermissionOptionKind.allow_once,
        g.PermissionOptionKind.reject_once,
    ]
    selected = g.RequestPermissionResponse.model_validate(
        {"outcome": {"outcome": "selected", "optionId": "allow-once"}}
    )
    cancelled = g.RequestPermissionResponse.model_validate({"outcome": {"outcome": "cancelled"}})
    assert isinstance(selected.outcome, g.SelectedPermissionOutcome)
    assert selected.outcome.option_id == "allow-once"
    assert cancelled.outcome.outcome == "cancelled"


# ─────────────────────────── 兼容性 ───────────────────────────


def test_unknown_fields_are_kept_for_forward_compatibility() -> None:
    """★ 新版本 agent 多带的字段不能丢：转交给 server 时要原样（§3.8 B2）。"""
    resp = g.PromptResponse.model_validate({"stopReason": "end_turn", "futureField": {"x": 1}})
    assert resp.model_dump(by_alias=True)["futureField"] == {"x": 1}


def test_wire_names_are_camel_case() -> None:
    dumped = g.PromptRequest(session_id="s1", prompt=[]).model_dump(
        by_alias=True, exclude_none=True
    )
    assert dumped == {"sessionId": "s1", "prompt": []}


# ─────────────────────────── 方法名 ───────────────────────────


def test_method_names_from_meta() -> None:
    assert methods.SESSION_PROMPT == "session/prompt"
    assert methods.SESSION_REQUEST_PERMISSION == "session/request_permission"
    assert methods.CANCEL_REQUEST == "$/cancel_request"
    assert methods.PROTOCOL_VERSION == 1
