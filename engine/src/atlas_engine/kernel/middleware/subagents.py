"""子智能体委派中间件 —— `task` 工具的唯一形态。

Atlas 的子智能体**只有一种执行方式**：`task` 把 (任务书, 名字, fresh) 交给
注入的 `DelegationProtocol` 受理方，由它在子智能体自己的会话上起一个子 run
（detail/subagent.html）。曾经并存的另外三种形态已整体删除：

  图内 SubAgent          在主图协程里编译并 ainvoke 子图。每次委派从空上下文
                         开始，且需要一套**平行的**治理装配（子图审批 / 子图
                         步数刹车 / 子图文件白名单）—— 与子 run 天然继承的
                         同一批守卫重复，两份语义必然漂移
  CompiledSubAgent       调用方自带 runnable。同上，且状态合并规则
                         （_EXCLUDED_STATE_KEYS / private_state_keys 两层
                         黑名单）只为它存在
  AsyncSubAgent          经 langgraph_sdk 跑在 LangSmith 部署上 —— Atlas
                         不用 LangSmith

多一种执行形态就多一套要维护的治理语义。kernel 在这里只剩两件事：
工具的形态（schema / 描述），和把受理方的返回整形成 ToolMessage。
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, NotRequired, TypedDict

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import BaseTool
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from atlas_engine.contracts import DelegationProtocol
from atlas_engine.kernel.middleware._utils import append_to_system_message

#: 委派工具在模型侧的名字。runner 的事件推导（subagent.started/finished 的
#: 判别）与 tool_registry 的目录都引用这里 —— 单一来源，改名只改一处。
SUBAGENT_TOOL = "task"

__all__ = [
    "SESSION_TASK_TOOL_DESCRIPTION",
    "SUBAGENT_TOOL",
    "SubAgent",
    "SubAgentMiddleware",
    "TaskToolSchema",
]


class SubAgent(TypedDict):
    """`task` 工具需要认识的子智能体信息 —— 只有名字和描述。

    模型解析、中间件栈、权限、技能全都不在这里：那些属于子 run 的装配
    （server 侧按 spec_for_subagent 派生的 AgentSpec 重新组装）。kernel
    只需要能把「有哪些子智能体、各自干什么」写进工具描述。
    """

    name: str
    """唯一标识。主 agent 调 `task` 时用它选人。"""

    description: str
    """干什么的。主 agent 据此决定何时委派。"""

    system_prompt: NotRequired[str]
    """子智能体的提示词。kernel **不使用** —— 它在子 run 里生效。
    调用方（atlas_engine.agent）顺手传了它，留作调试时一眼可见。"""


class TaskToolSchema(BaseModel):
    """Input schema for the `task` tool."""

    description: str = Field(
        description=(
            "A detailed description of the task for the subagent to perform autonomously. "
            "Include all necessary context and specify the expected output format."
        )
    )

    subagent_type: str = Field(description=("The type of subagent to use. Must be one of the available agent types listed in the tool description."))

    fresh: bool = Field(
        default=False,
        description=(
            "Start this subagent from a clean context instead of resuming its session. "
            "Default false: a subagent remembers what you asked it before, which is "
            "usually what you want. Set true only when the previous exchange sent it "
            "down a wrong path and you want it to reconsider from scratch."
        ),
    )


SESSION_TASK_TOOL_DESCRIPTION = """Delegate a complex, multi-step task to a subagent that keeps its own session.

Available agent types and the tools they have access to:
{available_agents}

Specify subagent_type to select the agent. Usage notes:
- Each subagent **remembers your previous delegations to it**. Refer back to earlier work instead of restating it; you only need to supply what is new.
- Delegation costs a full agent run of its own. Do it when the task is genuinely multi-step or needs its own context window, not to save yourself a couple of tool calls.
- Parallel delegation only works across **different** subagents. Two `task` calls to the same subagent in one turn are rejected: it holds one session, and that session is serial.
- The subagent's report is not shown to the user; relay a summary yourself.
- Tell the subagent whether to create content, analyze, or only research, since it cannot see the user's intent.
- Pass fresh=true to discard its session and start over — for when the previous exchange led it astray."""  # noqa: E501
"""Task-tool description for the **delegating** (session-level) mode.

The default `TASK_TOOL_DESCRIPTION` says "Each invocation is stateless", which
is exactly backwards for session-scoped subagents. Getting that wrong has
concrete costs: the model re-states the whole background in every task brief
(paying for it twice), or assumes it can fan out several calls to the same
subagent (which the session lock rejects).
"""


def _agents_block(subagents: Sequence[SubAgent]) -> str:
    return "\n".join(f"- {s['name']}: {s['description']}" for s in subagents)


def _build_delegating_task_tool(
    subagents: Sequence[SubAgent],
    gateway: DelegationProtocol,
    task_description: str | None,
) -> BaseTool:
    """委派模式的 `task`：不跑子图，只调 `delegate` 回调。

    ★ 与图内模式最重要的差别不是「谁来跑」，而是**子智能体图根本不编译**。
      会话级子智能体跑在自己的子 run 里，那条链路会用子会话的 spec 重新
      装配一遍 —— 在这里再编译一份，既白建一个图，也会让「哪份配置真正
      生效」有两个答案。所以这里只用到 spec 的 name / description。

    返回值是子智能体的最终文本，直接作为 ToolMessage 内容。没有 Command、
    没有状态合并：子会话的状态归子会话，主线程只看见结论（设计 §02）。
    """
    names = [spec["name"] for spec in subagents]
    block = _agents_block(subagents)
    if task_description is None:
        description = SESSION_TASK_TOOL_DESCRIPTION.format(available_agents=block)
    elif "{available_agents}" in task_description:
        description = task_description.format(available_agents=block)
    else:
        description = task_description

    def _unknown(subagent_type: str) -> str:
        allowed = ", ".join(f"`{n}`" for n in names)
        return f"We cannot invoke subagent {subagent_type} because it does not exist, the only allowed types are {allowed}"

    async def atask(description: str, subagent_type: str, fresh: bool = False) -> str:
        if subagent_type not in names:
            return _unknown(subagent_type)
        # ★ 委派失败（撞并发上限、同轮重名、子 run 失败）回成**工具错误文本**，
        #   不冒泡。主 agent 因此能改派、合并任务或自己动手 —— 而异常会让
        #   整个 run 失败，把一次可恢复的局部问题升级成全局故障。
        try:
            return await gateway.delegate(description, subagent_type, fresh=fresh)
        except Exception as exc:  # noqa: BLE001
            return f"委派给 {subagent_type} 失败：{exc}"

    def task(description: str, subagent_type: str, fresh: bool = False) -> str:
        # 同步路径在 Atlas 里不会被走到（图跑在 asyncio 上），但工具必须
        # 两条都给 —— 缺了同步版的话 StructuredTool 在同步调用时报的是
        # 一个与委派无关的内部错误。
        msg = "会话级子智能体只支持异步委派；请在异步图里调用 task。"
        raise NotImplementedError(msg)

    return StructuredTool.from_function(
        name=SUBAGENT_TOOL,
        func=task,
        coroutine=atask,
        description=description,
        infer_schema=False,
        args_schema=TaskToolSchema,
    )


class SubAgentMiddleware(AgentMiddleware[Any, ContextT, ResponseT]):
    """把 `task` 工具装进主图。

    受理方（delegate）是**必填**的：kernel 不再有图内执行的回落路径。
    没有受理方却配了子智能体，正确的失败点在装配时（engine 的 build_agent
    会先以 InvalidSpec 拦下；这里的 ValueError 是 kernel 直用时的兜底）。
    """

    def __init__(
        self,
        *,
        subagents: Sequence[SubAgent],
        delegate: DelegationProtocol,
        system_prompt: str | None = None,
        task_description: str | None = None,
    ) -> None:
        super().__init__()
        if not subagents:
            msg = "At least one subagent must be specified"
            raise ValueError(msg)
        if delegate is None:
            msg = "SubAgentMiddleware 需要 DelegationProtocol 受理方 —— 图内执行已删除"
            raise ValueError(msg)

        self.tools = [_build_delegating_task_tool(subagents, delegate, task_description)]

        # 附加到系统提示词的说明（可选）—— 子智能体名录已在工具描述里，
        # 这里只用于调用方想额外强调的委派方针。
        if system_prompt is not None:
            self.system_prompt = system_prompt + "\n\nAvailable subagent types:\n\n" + _agents_block(subagents)
        else:
            self.system_prompt = None

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Update the system message to include instructions on using subagents."""
        if self.system_prompt is not None:
            new_system_message = append_to_system_message(request.system_message, self.system_prompt)
            return handler(request.override(system_message=new_system_message))
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """(async) Update the system message to include instructions on using subagents."""
        if self.system_prompt is not None:
            new_system_message = append_to_system_message(request.system_message, self.system_prompt)
            return await handler(request.override(system_message=new_system_message))
        return await handler(request)
