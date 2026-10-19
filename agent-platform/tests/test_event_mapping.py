"""LangGraph updates 流 → TraceEvent 的映射（P4）。

★ 这里的输入形状**取自对真实 kernel 图的实测**（见提交信息里的探针），
不是照文档猜的。单测锁住映射，真实工具链路另由 test_live_gateway 兜底。
"""

from __future__ import annotations

from atlas_agent.domain.translator import tool_calls_from, tool_result_from
from langchain_core.messages import AIMessage, ToolMessage


def test_tool_calls_from_ai_message() -> None:
    msg = AIMessage(
        content="",
        tool_calls=[{"name": "write_todos", "args": {"todos": ["a", "b"]}, "id": "toolu_01"}],
    )
    calls = tool_calls_from(msg)
    assert len(calls) == 1
    assert calls[0]["call_id"] == "toolu_01"
    assert calls[0]["name"] == "write_todos"
    assert "todos=" in calls[0]["args_preview"]


def test_tool_calls_from_plain_message_is_empty() -> None:
    assert tool_calls_from(AIMessage(content="没有工具调用")) == []


def test_args_preview_is_truncated() -> None:
    """收起态不能塞进整个 SQL / 文件内容。"""
    msg = AIMessage(
        content="",
        tool_calls=[{"name": "write_file", "args": {"content": "x" * 5000}, "id": "t1"}],
    )
    preview = tool_calls_from(msg)[0]["args_preview"]
    assert len(preview) <= 120
    assert tool_calls_from(msg)[0]["args"]["content"] == "x" * 5000  # 原始值仍保留


def test_tool_result_success() -> None:
    msg = ToolMessage(content="ok", tool_call_id="toolu_01", name="write_todos")
    res = tool_result_from(msg)
    assert res is not None
    assert res["call_id"] == "toolu_01"
    assert res["status"] == "success"


def test_tool_result_error_is_distinguishable() -> None:
    msg = ToolMessage(content="boom", tool_call_id="t2", name="read_file", status="error")
    res = tool_result_from(msg)
    assert res is not None and res["status"] == "error"


def test_tool_result_from_non_tool_message_is_none() -> None:
    assert tool_result_from(AIMessage(content="hi")) is None
