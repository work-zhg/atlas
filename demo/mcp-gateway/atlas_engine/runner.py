"""跑一轮：建模型 → ToolSpec 转成 LangChain 工具 → 组装图 → 驱动循环 → 交回中立的结果。

★ 中间件是 engine 的扩展点（Atlas 的审批、延迟加载、步数上限、并发上限都在这一层）。
  这里只放一个 TraceMiddleware：打印每次调模型、每次执行工具。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from hops import hop
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from atlas_engine.contracts import ModelSpec, ToolSpec, TurnResult
from atlas_engine.models import build_model
from atlas_engine.tools import to_langchain


class TraceMiddleware(AgentMiddleware):
    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        hop("engine", f"调模型：{len(request.messages)} 条消息、{len(request.tools)} 个工具")
        return await handler(request)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        call = request.tool_call
        hop("engine", f"执行工具 {call['name']}({call['args']}) → ToolSpec.call")
        return await handler(request)


async def run_turn(model: ModelSpec, tools: list[ToolSpec], question: str) -> TurnResult:
    """用户消息进去，跑到模型不再发 tool_call 为止。"""
    graph = create_agent(
        model=build_model(model),
        tools=[to_langchain(t) for t in tools],
        middleware=[TraceMiddleware()],
    )
    result = await graph.ainvoke({"messages": [HumanMessage(question)]})
    hop("engine", "本轮结束：模型没有再发 tool_call")
    messages = result["messages"]
    return TurnResult(answer=messages[-1].text, transcript=[(m.type, m.text) for m in messages])
