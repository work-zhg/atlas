"""挂起与续跑的**唯一**图内支点。

一轮可以分多段执行：中间停下来等一个外部事件（子智能体的结论、用户的批准），
事件到了再从上次的地方接着跑。图内只看 state 里的两个字段
（contracts/suspension.py）：

    waiting_on 为空                          → 正常流向 model
    有一项的 token 已经在 decisions 里       → jump_to "tools"（去执行 / 填结论）
                                               并行的兄弟调用已有结果 → wrap_tool_call 原样返回，不重跑
    都还没有结果                             → jump_to "end"（本段到此为止）

这个中间件因此不需要知道 SubAgent 和审批的存在 —— 它们是 waiting_on 的两个生产者，
各自在拿到结果时把自己那一项删掉（PermissionMiddleware / SubAgentMiddleware）。

★ 为什么落在 `before_model` 而不是「工具执行之后」。middleware 没有 after_tools
  钩子。工具节点的出边指向第一个 before_model 节点 —— 那里正是「整批工具执行
  完、下一次模型调用之前」，是唯一能既看到全部工具结果、又还来得及不调模型的
  位置。

★ 为什么不用 checkpointer + interrupt。我们不需要恢复图的内存状态 —— 图的状态
  就是 messages + waiting_on，前者的事实源是 message 表，后者是 run.waiting_on。
  需要的只是「这一段从哪个节点开始」，而 `jump_to` 是 langchain middleware 的
  图内条件边，已经给了。引入 checkpointer 会带来第二个状态源，而本仓四处注释
  明确拒绝过（doc/detail/suspension.html §03，附实验输出）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    OmitFromOutput,
    hook_config,
)
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from atlas_engine.contracts.suspension import WaitingOn, merge_waits

__all__ = ["SuspensionMiddleware", "SuspensionState", "release_wait"]


class SuspensionState(AgentState):
    """挂起相关的两个 state 字段。语义见 contracts/suspension.py。

    凡是读写它们的中间件都声明这个 schema（Patch / 审批 / 子智能体 / 本中间件）——
    合并时重复声明没有副作用，写在每个上面是为了让读代码的人知道「它依赖这些字段」。
    """

    #: 不加可见性标注：挂起时 runner 要从最终 state 里取出来落库，续跑时原样交回。
    waiting_on: NotRequired[Annotated[WaitingOn, merge_waits]]
    #: 只进不出：续跑时由 server 交进来，不需要再带回去。
    #: approval → ApprovalOutcome（结构体）；subagent → 结论文本
    decisions: NotRequired[Annotated[dict[str, Any], OmitFromOutput]]
    #: 这一段属于哪个 run（runner 每段交进来）。审批 / 提问的 id 由 (run_id, tool_call_id) 派生
    #: （approval.approval_id_for）—— 没有它的话不同 run 里同名的 tool_call_id 会撞成同一条审批
    run_id: NotRequired[Annotated[str, OmitFromOutput]]


class SuspensionMiddleware(AgentMiddleware):
    """让图在「有东西在等」时干净地跳出，在「等到了」时直接去 tools。"""

    state_schema = SuspensionState

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
        waits = state.get("waiting_on") or {}
        if not waits:
            return None

        # ① 等到了 → 去 tools。那些调用还悬空着（图入口的 Patch 认 waiting_on，
        #    没有补 cancelled），ToolNode 会执行最后一条 AIMessage 的全部调用：
        #    有结果的由各自的中间件填进去并删掉 waiting_on 项，已完成的兄弟由下面的
        #    wrap_tool_call 原样返回。
        #
        #    ★ 只要有**一项**等到了就去，不等全部：barrier 本来就要求全部结清才续跑；
        #      没等到的那项在 tools 里会被重新登记，回到这里时走 ②。
        decisions = state.get("decisions") or {}
        if any((wait or {}).get("token") in decisions for wait in waits.values()):
            return {"jump_to": "tools"}

        # ② 还在等 → 本段到此为止，不调模型。
        #
        #    不跳出的后果是模型拿到悬空的 tool_call —— provider 直接 400
        #    （每个 tool_use 要恰好一条 tool_result）。
        return {"jump_to": "end"}

    # ★ jump_to "tools" 时 LangChain 把整个 state 交给 ToolNode，ToolNode 会把最后一条
    #   AIMessage 的**所有** tool_call 再执行一遍（正常路径是 Send 只发未完成的那几个）。
    #   并行的一批里只有一个在等时，已完成的兄弟调用就会重跑：副作用两次，还多一份
    #   tool_result，下一次模型调用被拒（each tool_use must have a single result）。
    #   这里拦下「本批已有结果」的调用，原样返回那条结果 —— 消息按 id 去重，不会重复。
    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        done = _completed_result(request)
        return done if done is not None else handler(request)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        done = _completed_result(request)
        return done if done is not None else await handler(request)


def release_wait(result: ToolMessage | Command[Any], tool_call_id: str) -> Command[Any]:
    """把「这个调用拿到结果了」和「从 waiting_on 里删掉它」放进同一个更新。

    ★ 必须删：留着的话回到 before_model 时它的 token 仍在 decisions 里，
      于是再跳一次 tools —— 来回跳到 GraphRecursionError。

    ★ wrap_tool_call 可以返回不带 ToolMessage 的 Command（ToolNode 只检查工具
      **自己**返回的 Command），所以挂起时只登记 waiting_on、不写占位消息是成立的；
      这里则是反过来，结果与删除一起交出去。

    ★ 内层给同一个调用登记了**新的**等待项时，以内层为准：`task` 被配成需审批，
      批准后 SubAgentMiddleware 起了子 run —— 等待从 approval 换成 subagent，
      不能被这里的删除抹掉（抹掉的话调用悬空且无人登记，Patch 会补成 cancelled）。
    """
    release = {tool_call_id: None}
    if not isinstance(result, Command):
        return Command(update={"messages": [result], "waiting_on": release})
    update = result.update
    if isinstance(update, dict):
        merged: Any = {**update, "waiting_on": {**release, **(update.get("waiting_on") or {})}}
    else:  # Command.update 也可以是 (key, value) 序列；按顺序过 reducer，后写的覆盖先写的
        merged = [("waiting_on", release), *(update or ())]
    return Command(graph=result.graph, update=merged, resume=result.resume, goto=result.goto)


def _completed_result(request: Any) -> ToolMessage | None:
    """本批里这个 tool_call 已有的结果；没有返回 None。"""
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
        if message.tool_call_id == call_id:
            return message
    return None
