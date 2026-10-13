"""挂起与续跑的**唯一**图内支点。

一轮可以分多段执行：中间停下来等一个外部事件（子智能体的结论、用户的批准），
事件到了再从上次的地方接着跑。整件事在图里只需要三条判据：

    最后一批里有「已放行」占位   → 删掉它 + jump_to "tools"（去执行那个工具）
                                  并行的兄弟调用已有结果 → wrap_tool_call 直接返回原结果，不重跑
    最后一批里有挂起哨兵         → jump_to "end"（本段到此为止）
    两者都没有                   → 正常流向 model

全部只看 `state["messages"]`。这个中间件因此是**无状态**的，也不需要知道委派
和审批的存在 —— 它们只是 marker 的两个生产者（contracts/suspension.py）。

★ 为什么落在 `before_model` 而不是「工具执行之后」。middleware 没有 after_tools
  钩子。工具节点的出边指向第一个 before_model 节点 —— 那里正是「整批工具执行
  完、下一次模型调用之前」，是唯一能既看到全部工具结果、又还来得及不调模型的
  位置。

★ 为什么不用 checkpointer + interrupt。我们不需要恢复图的内存状态 —— 图的状态
  就是 messages，而 messages 的事实源是 message 表。需要的只是「这一段从哪个
  节点开始」，而 `jump_to` 是 langchain middleware 的图内条件边，已经给了。
  引入 checkpointer 会带来第二个状态源，而本仓四处注释明确拒绝过
  （doc/detail/suspension.html §03，附实验输出）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain_core.messages import RemoveMessage, ToolMessage

from atlas_engine.contracts.suspension import is_released, parse_suspension

__all__ = ["SuspensionMiddleware"]


class SuspensionMiddleware(AgentMiddleware):
    """让图在「有东西在等」时干净地跳出，在「有活没干完」时补上。"""

    #: ★ `can_jump_to` 里声明 "tools" 要求图里**真的有** tools 节点 —— 否则
    #:   LangGraph 在编译时就报 "found unknown target 'tools'"，而那句错误离
    #:   根因（某个 agent 一个工具都没配）很远。
    #:
    #:   生产路径安全：create_deep_agent 总会装内置工具（write_todos 等），
    #:   所以 tools 节点必然存在 —— 即使 spec 的 tool_names 是空的。
    #:   这条前提由 tests/test_suspension_middleware.py 钉着，将来 kernel 改成
    #:   「没勾工具就不装 tools 节点」时会先在那里红，而不是在某次部署时炸。
    @hook_config(can_jump_to=["tools", "end"])
    def before_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        released, suspended = _scan_last_batch(state.get("messages") or [])

        # ① 等到了 → 删掉占位，去执行那个工具。
        #
        #    ★ 删除**必须在这里**做，不能在 server 重建历史时做。kernel 的
        #      PatchToolCallsMiddleware 在图入口（before_agent）会给一切悬空的
        #      tool_call 补上一句「was cancelled」，而它排在核心栈里、顺序
        #      无法调整。提前删出来的悬空会被它当场补掉 —— 于是这里看到的序列
        #      配对完整，不跳 tools，流向模型，模型看到「工具被取消了」就重新
        #      发一次调用，再挂起一次：**死循环**。
        #      推到 before_model 之后 patch 已经跑完，没有第二次插手的机会。
        if released is not None:
            return {"messages": [RemoveMessage(id=released)], "jump_to": "tools"}

        # ② 还在等 → 本段到此为止，不调模型。
        #
        #    不跳出的后果是模型看到 `__ATLAS_SUSPENDED__:...` 并据此往下推理 ——
        #    它会把哨兵当成子智能体的结论，然后一本正经地汇报一个不存在的结果。
        if suspended:
            return {"jump_to": "end"}

        return None

    # ★ jump_to "tools" 时 LangChain 把整个 state 交给 ToolNode，ToolNode 会把最后一条
    #   AIMessage 的**所有** tool_call 再执行一遍（正常路径是 Send 只发未完成的那几个）。
    #   并行的一批里只有一个在等时，已完成的兄弟调用就会重跑：副作用两次，还多一份
    #   tool_result，下一次模型调用被拒（each tool_use must have a single result）。
    #   这里拦下「本批已有真实结果」的调用，原样返回那条结果 —— 消息按 id 去重，不会重复。
    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        done = _completed_result(request)
        return done if done is not None else handler(request)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        done = _completed_result(request)
        return done if done is not None else await handler(request)


def _completed_result(request: Any) -> ToolMessage | None:
    """本批里这个 tool_call 已有的真实结果（不是挂起哨兵 / 放行占位）；没有返回 None。"""
    state = getattr(request, "state", None)
    messages = state.get("messages") if isinstance(state, dict) else None
    if not messages:
        return None
    call_id = (getattr(request, "tool_call", None) or {}).get("id")
    if not call_id:
        return None
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            break  # 只看最后一批
        if message.tool_call_id != call_id:
            continue
        content = message.content if isinstance(message.content, str) else ""
        if is_released(content) or parse_suspension(content) is not None:
            return None
        return message
    return None


def _scan_last_batch(messages: list[Any]) -> tuple[str | None, bool]:
    """扫**最后一批** ToolMessage。返回 (已放行占位的 id, 有没有挂起哨兵)。

    ★ 只看最后一批：从尾部往前数到第一个非 ToolMessage 就停。更早的标记属于
      已经处理过的段 —— 再动一次只会让 run 在原地打转。
    """
    released: str | None = None
    suspended = False
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            break
        if is_released(message.content) and released is None:
            released = message.id
        elif parse_suspension(message.content) is not None:
            suspended = True
    return released, suspended
