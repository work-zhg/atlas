"""智能体图里的三个中间件，以及 task 工具。

主智能体和 SubAgent 装的是同一套，所以 SubAgent 也能继续派 SubAgent。
图这一侧不认识数据库，只认 SubAgentSpawner 一个协议。

    PatchToolCallsMiddleware  图入口：给被放弃的悬空 tool_call 补 cancelled；在等的不补
    SuspensionMiddleware      before_model：在等 → jump_to end；等到了 → jump_to tools
                              awrap_tool_call：续跑时已经有结果的兄弟调用原样返回
    SubAgentMiddleware        task 工具的 awrap_tool_call：
                                首次 → 起子 run、登记 waiting_on、不写结果
                                续跑 → 把结论当工具结果返回，并删掉那一项 waiting_on

前两个和 demo/suspend-resume 的同名中间件一样，第三个对应那边的 ApprovalMiddleware：
挂起机制只有一套，SubAgent 和审批只是 waiting_on 的两种 reason。
"""

from __future__ import annotations

from typing import Any, Protocol

from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, BaseMessage, RemoveMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command
from state import SubAgentState
from tracing import say


class SubAgentSpawner(Protocol):
    """起一个 SubAgent 的子 run（server 侧实现，见 subagent_service.py）。

    只负责「起」：建好子 run、交给执行器就返回它的 id，不等它跑完。
    """

    async def spawn(self, name: str, task: str, call_id: str) -> str: ...


class PatchToolCallsMiddleware(AgentMiddleware):
    """图入口：给被放弃的悬空 tool_call 补一条 cancelled 结果；登记在 waiting_on 里的不补。

    悬空有两种来源，只有后一种该补：
      在等    SubAgentMiddleware 起了子 run 后故意不写结果，并登记在 waiting_on 里
      被放弃  比如进程在工具执行到一半时退出，结果没来得及写
    在等的也补上的话，续跑时这些调用永远等不到结论（README 实验 3）。
    """

    state_schema = SubAgentState

    def before_agent(self, state: SubAgentState, runtime: Any = None) -> dict[str, Any] | None:
        messages = state["messages"]
        waiting = set(state.get("waiting_on") or {})
        answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        patched: list[BaseMessage] = []
        for message in messages:
            patched.append(message)
            if not isinstance(message, AIMessage):
                continue
            for call in message.tool_calls:
                if call["id"] in answered or call["id"] in waiting:
                    continue
                say(f"patch：{call['id']} 悬空，且不在 waiting_on 里 → 补 cancelled")
                cancelled = f"Tool call {call['name']} with id {call['id']} was cancelled."
                patched.append(ToolMessage(cancelled, tool_call_id=call["id"]))
        if len(patched) == len(messages):
            return None
        # 补的结果要紧跟在对应的 ai 消息后面，所以整体替换
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *patched]}


class SuspensionMiddleware(AgentMiddleware):
    """唯一的图内判断点：只看 state 里的 waiting_on 和 results。"""

    state_schema = SubAgentState

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """续跑时 jump_to tools，ToolNode 会把最后一条 ai 消息里的调用**全部**再执行一遍。

        ★ 同一批里已经有结果的兄弟调用（比如派不出去、当场回了文本的）原样返回，不重跑：
          同一个 id 的消息会被替换而不是追加，结果还是恰好一条（README 实验 7）。
        """
        call_id = request.tool_call["id"]
        for message in request.state["messages"]:
            if isinstance(message, ToolMessage) and message.tool_call_id == call_id:
                say(f"{call_id} 在上一段已经有结果 → 原样返回，不重跑")
                return message
        return await handler(request)

    @hook_config(can_jump_to=["tools", "end"])
    def before_model(self, state: SubAgentState, runtime: Any = None) -> dict[str, Any] | None:
        waits = state.get("waiting_on") or {}
        if not waits:
            return None
        results = state.get("results") or {}
        if any(w["token"] in results for w in waits.values()):
            # 等到了：这些调用还悬空着（入口的 Patch 没有补），jump 到 tools 再执行一遍
            say(f"before_model：{list(waits)} 的结论都到了 → jump_to tools")
            return {"jump_to": "tools"}
        say(f"before_model：在等 {list(waits)} → jump_to end（不调模型，本段结束）")
        return {"jump_to": "end"}


def task_tool(subagents: dict[str, str]) -> BaseTool:
    listing = "\n".join(f"- {name}: {desc}" for name, desc in subagents.items())

    @tool(
        "task",
        description=f"把一件多步骤的活交给 SubAgent，只拿回它的结论。可用的 SubAgent：\n{listing}",
    )
    async def task(subagent_type: str, description: str) -> str:
        raise RuntimeError("task 不会被直接执行：SubAgentMiddleware 在 awrap_tool_call 里接管")

    return task


class SubAgentMiddleware(AgentMiddleware):
    """task 工具的全部行为都在这里。工具本身只提供 schema，函数体永远不会被执行。"""

    state_schema = SubAgentState

    def __init__(self, spawner: SubAgentSpawner, subagents: dict[str, str]) -> None:
        super().__init__()
        self.spawner = spawner
        self.subagents = subagents
        self.tools = [task_tool(subagents)]

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        call = request.tool_call
        if call["name"] != "task":
            return await handler(request)
        call_id = call["id"]
        wait = (request.state.get("waiting_on") or {}).get(call_id)
        results = request.state.get("results") or {}

        if wait is not None and wait["token"] in results:
            # 续跑：结论当工具结果，并把这一项从 waiting_on 里删掉（None = 删除）
            say(f"{call_id} 的子 run {wait['token']} 已结束 → 结论作为工具结果")
            result = ToolMessage(results[wait["token"]], tool_call_id=call_id, name="task")
            return Command(update={"messages": [result], "waiting_on": {call_id: None}})

        name = call["args"]["subagent_type"]
        if name not in self.subagents:
            # ★ 派不出去的直接回成文本，不挂起：模型看到后可以改派或自己动手
            say(f"{call_id} → SubAgent {name} 不存在 → 当场回成工具结果，不挂起")
            allowed = "、".join(self.subagents)
            return ToolMessage(f"SubAgent {name} 不存在，可用的是：{allowed}", tool_call_id=call_id)

        sub_run_id = await self.spawner.spawn(name, call["args"]["description"], call_id)
        # ★ 不写 tool 消息：call 留着悬空，登记在 waiting_on 里。配对规则只约束交给模型的消息，
        #   而 before_model 接下来会直接 jump_to end，不调模型
        return Command(
            update={"waiting_on": {call_id: {"reason": "subagent", "token": sub_run_id}}}
        )
