"""Middleware to patch dangling tool calls in the messages history."""

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, AnyMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from atlas_engine.kernel.middleware.suspension import SuspensionState


class PatchToolCallsMiddleware(AgentMiddleware):
    """Middleware to patch dangling tool calls in the messages history.

    ★ 悬空有两种来源，只有后一种该补（contracts/suspension.py）：

        在等    审批 / SubAgent 挂起时故意不写结果，登记在 state.waiting_on 里
        被放弃  比如进程在工具执行到一半时退出，结果没来得及写

      在等的也补上的话，续跑执行工具后同一个调用就有两份结果 —— 下一次模型调用
      被拒（each tool_use must have a single result）。
    """

    state_schema = SuspensionState

    def before_agent(self, state: SuspensionState, runtime: Runtime[Any]) -> dict[str, Any] | None:  # noqa: ARG002
        """Before the agent runs, handle dangling tool calls from any AIMessage."""
        messages = state["messages"]
        if not messages:
            return None

        answered_ids = {msg.tool_call_id for msg in messages if msg.type == "tool"}
        skip = answered_ids | set(state.get("waiting_on") or {})

        if not any(
            tool_call["id"] is not None and tool_call["id"] not in skip
            for msg in messages
            if isinstance(msg, AIMessage)
            for tool_call in (*msg.tool_calls, *msg.invalid_tool_calls)
        ):
            return None

        patched_messages: list[AnyMessage] = []
        for msg in messages:
            patched_messages.append(msg)
            if not isinstance(msg, AIMessage):
                continue
            for tool_call in (*msg.tool_calls, *msg.invalid_tool_calls):
                tool_call_id = tool_call["id"]
                if tool_call_id is None or tool_call_id in skip:
                    continue
                name = tool_call["name"] or "unknown"
                if tool_call.get("type") == "invalid_tool_call":
                    content = f"Tool call {name} with id {tool_call_id} could not be executed - arguments were malformed or truncated."
                else:
                    content = f"Tool call {name} with id {tool_call_id} was cancelled - another message came in before it could be completed."
                patched_messages.append(ToolMessage(content=content, name=name, tool_call_id=tool_call_id))

        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *patched_messages]}
