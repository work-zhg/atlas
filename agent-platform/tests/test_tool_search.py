"""工具按需加载（doc/context-management-design.html §13）。"""

from __future__ import annotations

from typing import Any

from atlas_engine.kernel.middleware.tool_search import TOOL_SEARCH, ToolSearchMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool


def _tool(name: str, desc: str) -> StructuredTool:
    return StructuredTool.from_function(func=lambda q: q, name=name, description=desc)


TOOLS = [
    _tool("mcp_jira_create_issue", "在 Jira 里创建 issue"),
    _tool("mcp_jira_search", "搜索 Jira issue"),
    _tool("mcp_mail_send", "发送邮件"),
]


def test_search_by_keyword_and_select() -> None:
    mw = ToolSearchMiddleware(TOOLS)
    assert [t.name for t in mw.search("jira")][:2] == ["mcp_jira_create_issue", "mcp_jira_search"]
    assert [t.name for t in mw.search("发送邮件")] == ["mcp_mail_send"]
    assert [t.name for t in mw.search("select:mcp_mail_send,nope")] == ["mcp_mail_send"]
    out = mw.tools[0].invoke({"query": "邮件"})
    assert "[loaded] mcp_mail_send" in out


async def test_unloaded_tools_are_hidden_and_catalog_listed() -> None:
    mw = ToolSearchMiddleware(TOOLS)
    native = _tool("read_file", "读文件")
    seen: list[Any] = []

    async def handler(req: ModelRequest) -> ModelResponse:
        seen.append(req)
        return ModelResponse(result=[AIMessage(content="ok")])

    req = ModelRequest(
        model=None,  # type: ignore[arg-type]
        messages=[HumanMessage(content="hi")],
        system_message=SystemMessage(content="你是助手"),
        tools=[native, *TOOLS, mw.tools[0]],
        state={},  # type: ignore[arg-type]
    )
    await mw.awrap_model_call(req, handler)
    names = [t.name for t in seen[0].tools]
    assert names == ["read_file", TOOL_SEARCH]
    assert (
        "【延迟工具】" in seen[0].system_message.content
        and "mcp_mail_send" in seen[0].system_message.content
    )

    # 历史里有 tool_search 的结果 → 对应工具带上完整定义
    loaded = ToolMessage(
        content="找到 1 个\n[loaded] mcp_mail_send", tool_call_id="c", name=TOOL_SEARCH
    )
    await mw.awrap_model_call(req.override(messages=[HumanMessage(content="hi"), loaded]), handler)
    assert "mcp_mail_send" in [t.name for t in seen[1].tools]
    assert "mcp_jira_search" not in [t.name for t in seen[1].tools]


async def test_block_system_message_is_preserved() -> None:
    mw = ToolSearchMiddleware(TOOLS)
    seen: list[Any] = []

    async def handler(req: ModelRequest) -> ModelResponse:
        seen.append(req)
        return ModelResponse(result=[AIMessage(content="ok")])

    block = {"type": "text", "text": "原系统提示", "cache_control": {"type": "ephemeral"}}
    req = ModelRequest(
        model=None,  # type: ignore[arg-type]
        messages=[HumanMessage(content="hi")],
        system_message=SystemMessage(content=[block]),
        tools=[],
        state={},  # type: ignore[arg-type]
    )
    await mw.awrap_model_call(req, handler)
    content = seen[0].system_message.content
    assert content[0] == block and "延迟工具" in content[1]["text"]


def test_deferred_threshold() -> None:
    from atlas_server.config import Settings
    from atlas_server.executor.assembly import _deferred_mcp_tools

    s = Settings(deferred_tools_min_count=2, deferred_tools_min_tokens=10_000)
    assert _deferred_mcp_tools([], s) == []
    assert _deferred_mcp_tools(TOOLS[:2], s) == []
    assert [t.name for t in _deferred_mcp_tools(TOOLS, s)] == [t.name for t in TOOLS]
    s2 = Settings(deferred_tools_min_count=100, deferred_tools_min_tokens=10)
    assert len(_deferred_mcp_tools(TOOLS[:1], s2)) == 1  # 定义总量超阈值
