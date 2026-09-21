"""高风险工具的人工确认（文档 §12.2）。

拦截点选的是 langchain middleware 的 `awrap_tool_call`，不是 LangGraph 的
interrupt：
  · interrupt 要求 checkpointer，且会把整个图挂起再靠 Command(resume=) 恢复 ——
    那是另一套执行模型，与「一路 astream 到底」的流式链路不兼容
  · awrap_tool_call 只是在工具执行外面包一层 await，SSE 连接自始至终开着，
    等待期间还能继续发心跳

等待逻辑由调用方经 `contracts.ApprovalGate` 注入 —— kernel 不知道
Postgres 与 Redis 的存在。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from atlas_engine.contracts import ApprovalGate

__all__ = ["EXPIRED_RESULT", "REJECTED_RESULT", "ApprovalMiddleware"]

#: 被拒绝时回给 agent 的工具结果。★ 不是抛异常 ——
#: 让 agent 知道「这条路被挡了」，它可以换个方案继续（§12.2）。
REJECTED_RESULT = "用户拒绝执行该工具。请换一种方式完成任务，或说明为什么必须使用它。"
EXPIRED_RESULT = "等待人工确认超时，该工具未被执行。"


class ApprovalMiddleware(AgentMiddleware):
    """对 `require_approval_for` 里的工具，执行前先等人点头。"""

    def __init__(self, tool_names: frozenset[str], gate: ApprovalGate) -> None:
        super().__init__()
        self._guarded = tool_names
        self._gate = gate

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call or {}
        name = str(call.get("name", ""))
        if name not in self._guarded:
            return await handler(request)

        from langgraph.config import get_stream_writer

        approval_id = str(uuid.uuid4())
        args = call.get("args") or {}

        # 先把「需要确认」发出去，再阻塞等待 —— 顺序反了的话用户看不到弹窗，
        # 只会看到界面卡住不动。
        writer = get_stream_writer()
        writer(
            {
                "kind": "approval.required",
                "approval_id": approval_id,
                "tool_name": name,
                "args": args,
                "reason": f"{name} 在该智能体的高风险工具列表中",
            }
        )

        decision = await self._gate.request(approval_id=approval_id, tool_name=name, args=args)

        if decision == "approved":
            return await handler(request)

        return ToolMessage(
            content=REJECTED_RESULT if decision == "rejected" else EXPIRED_RESULT,
            tool_call_id=str(call.get("id", "")),
            name=name,
            status="error",
        )
