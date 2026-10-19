"""SubAgent 中间件 —— `task` 工具的唯一形态。

Atlas 的 SubAgent **只有一种执行方式**：`task` 让注入的 `SubAgentSpawner` 在
SubAgent 自己的会话上起一个子 run，本段随即挂起，子 run 跑完叫醒父 run、
续跑时把结论填进 `task` 的结果（detail/subagent.html、contracts/suspension.py）。
曾经并存的另外三种形态已整体删除：

  图内 SubAgent          在主图协程里编译并 ainvoke 子图。每次从空上下文
                         开始，且需要一套**平行的**治理装配（子图审批 / 子图
                         步数刹车 / 子图文件白名单）—— 与子 run 天然继承的
                         同一批守卫重复，两份语义必然漂移
  CompiledSubAgent       调用方自带 runnable。同上，且状态合并规则
                         （_EXCLUDED_STATE_KEYS / private_state_keys 两层
                         黑名单）只为它存在
  AsyncSubAgent          经 langgraph_sdk 跑在 LangSmith 部署上 —— Atlas
                         不用 LangSmith

多一种执行形态就多一套要维护的治理语义。kernel 在这里只剩两件事：
工具的形态（schema / 描述），以及 `task` 调用与 state.waiting_on 之间的转换
（SubAgentMiddleware.awrap_tool_call，与审批中间件同一个姿态）。
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal, NotRequired, TypedDict

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import BaseTool
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field

from atlas_engine.contracts import SubAgentSpawner
from atlas_engine.kernel.middleware._utils import append_to_system_message
from atlas_engine.kernel.middleware.suspension import SuspensionState, release_wait

#: SubAgent 工具在模型侧的名字。runner 的事件推导（subagent.started/finished 的
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
    """干什么的。模型据此决定何时派给它。"""

    system_prompt: NotRequired[str]
    """子智能体的提示词。kernel **不使用** —— 它在子 run 里生效。
    调用方（server 的 build_graph）顺手传了它，留作调试时一眼可见。"""

    session_mode: NotRequired[Literal["persistent", "ephemeral"]]
    """会话模式，缺省 persistent。只用来写工具描述：两种模式的「记不记得」与
    「能不能同轮并行」正好相反，描述写错的代价见 SESSION_TASK_TOOL_DESCRIPTION。"""


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
            "Only meaningful for [persistent] subagents: start from a clean context "
            "instead of resuming its session. Default false: a persistent subagent "
            "remembers what you asked it before, which is usually what you want. Set true "
            "only when the previous exchange sent it down a wrong path. [ephemeral] "
            "subagents always start fresh, so this flag has no effect on them."
        ),
    )


SESSION_TASK_TOOL_DESCRIPTION = """Delegate a complex, multi-step task to a subagent. It works in its own session with its own context window; only its final report comes back to you.

Available agent types (with session mode) and what they do:
{available_agents}

Session modes:
- [persistent] The subagent keeps one session and **remembers your previous delegations to it**. Refer back to earlier work and supply only what is new. Its session is serial: two `task` calls to the same persistent subagent in one turn are rejected. Pass fresh=true to discard its session when the previous exchange led it astray.
- [ephemeral] Every call starts a brand-new session with zero context, and nothing is remembered afterwards. You may call the same ephemeral subagent several times in one turn; the calls run in parallel (e.g. investigate several options at once).

Writing the brief — the subagent has not seen this conversation. Brief it like a smart colleague who just walked into the room:
- State the goal and the purpose, so it can calibrate depth ("this feeds a PR description — focus on user-facing changes"; "I need this to plan an implementation — report file paths and signatures").
- Say what you already know or have ruled out, so it does not redo it.
- Be concrete: paths, names, numbers. For a lookup hand over the exact command; for an investigation hand over the question rather than prescribed steps.
- Say whether it should create or modify things, or only research and report.
- Say what shape and length of report you want ("a punch list, under 200 words").
- **Never delegate understanding.** Do not write "based on your findings, fix it" or "implement what the research suggests". Read earlier reports yourself and write a brief that proves you understood: what to change, where, and why.

Usage notes:
- Delegation costs a full agent run. Use it when the task is genuinely multi-step or needs its own context window, not to save a couple of tool calls.
- Different subagents, or the same [ephemeral] subagent, can be called in parallel within one turn.
- The report is not shown to the user; relay what matters yourself.
- The result ends with a <subagent .../> line (status, tokens, tool calls, duration). If the subagent failed or was stopped, its last partial output is included — verify it before relying on it."""  # noqa: E501
"""Task-tool description for the session-level SubAgent mode.

The default `TASK_TOOL_DESCRIPTION` says "Each invocation is stateless", which
is exactly backwards for session-scoped subagents. Getting that wrong has
concrete costs: the model re-states the whole background in every task brief
(paying for it twice), or assumes it can fan out several calls to the same
subagent (which the session lock rejects).
"""


def _agents_block(subagents: Sequence[SubAgent]) -> str:
    return "\n".join(
        f"- {s['name']} [{s.get('session_mode') or 'persistent'}]: {s['description']}"
        for s in subagents
    )


def _build_task_tool(subagents: Sequence[SubAgent]) -> BaseTool:
    """`task` 工具：只提供 schema 与描述，函数体**永远不会被执行**。

    ★ 它的全部行为在 SubAgentMiddleware.awrap_tool_call 里（起子 run、登记
      waiting_on、续跑时填结论）—— 与审批中间件接管高风险工具是同一个姿态。
      工具函数本身做不到：ToolNode 要求工具返回的 Command 里必须带一条对应的
      ToolMessage，而起了子 run 之后恰恰**不该**写结果（调用要悬空，contracts/suspension.py）。

    ★ SubAgent 的图根本不编译：它跑在自己的子 run 里，那条链路会用 SubAgent 的
      spec 重新装配一遍 —— 在这里再编译一份，既白建一个图，也会让「哪份配置真正
      生效」有两个答案。所以这里只用到 spec 的 name / description。
    """
    description = SESSION_TASK_TOOL_DESCRIPTION.format(available_agents=_agents_block(subagents))

    def task(description: str, subagent_type: str, fresh: bool = False) -> str:
        msg = "task 不会被直接执行：SubAgentMiddleware 在 awrap_tool_call 里接管"
        raise RuntimeError(msg)

    async def atask(description: str, subagent_type: str, fresh: bool = False) -> str:
        return task(description, subagent_type, fresh)

    return StructuredTool.from_function(
        name=SUBAGENT_TOOL,
        func=task,
        coroutine=atask,
        description=description,
        infer_schema=False,
        args_schema=TaskToolSchema,
    )


class SubAgentMiddleware(AgentMiddleware[Any, ContextT, ResponseT]):
    """把 `task` 工具装进图，并接管它的全部行为。

        首次    名字不在名录里 → 当场回一条失败结果；否则 spawner 起子 run，
                在 waiting_on 里登记（reason=subagent），**不写结果** —— 本段随即挂起
        续跑    这个调用的结论已在 decisions 里 → 作为 `task` 的结果交出，删掉那一项

    ★ 它排在调用方中间件**之后**（create_deep_agent）：已经有结果的兄弟调用由外层的
      SuspensionMiddleware 原样返回，`task` 被配成需审批时由外层的 ApprovalMiddleware
      先放行 —— 起子 run 永远是最后一步，不会重复起、也不会绕过审批。

    ★ SubAgent 的 SubAgent：子 run 的图装的是同一个中间件（server 侧的
      spec_for_subagent 给 native SubAgent 留着名录），所以嵌套不设层数 —— 每一层都是
      「起子 run → 挂起 → 被叫醒 → 填结论」这同一件事。

    spawner 是**必填**的：kernel 没有图内执行的回落路径。没有 spawner 却配了
    SubAgent，正确的失败点在装配时（server 的 build_graph 先以 InvalidSpec 拦下；
    这里的 ValueError 是 kernel 直用时的兜底）。
    """

    state_schema = SuspensionState

    def __init__(
        self,
        *,
        subagents: Sequence[SubAgent],
        spawner: SubAgentSpawner,
        system_prompt: str | None = None,
    ) -> None:
        super().__init__()
        if not subagents:
            msg = "At least one subagent must be specified"
            raise ValueError(msg)
        if spawner is None:
            msg = "SubAgentMiddleware 需要 SubAgentSpawner —— 图内执行已删除"
            raise ValueError(msg)

        self._spawner = spawner
        self._names = [spec["name"] for spec in subagents]
        self.tools = [_build_task_tool(subagents)]

        # 附加到系统提示词的说明（可选）—— SubAgent 名录已在工具描述里，
        # 这里只用于调用方想额外强调的派发方针。
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

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call or {}
        if call.get("name") != SUBAGENT_TOOL:
            return await handler(request)
        call_id = str(call.get("id") or "")
        state = getattr(request, "state", None) or {}

        # 续跑：结论已到 → 作为工具结果交出，并从 waiting_on 里删掉这一项
        wait = (state.get("waiting_on") or {}).get(call_id)
        if wait is not None and wait.get("reason") == "subagent":
            result = (state.get("decisions") or {}).get(wait.get("token", ""))
            if result is None:
                # barrier 只在子 run 终态后续跑，走不到这里；真走到了就原样保持登记，
                # 而不是再起一个子 run
                return Command(update={"waiting_on": {call_id: wait}})
            message = ToolMessage(content=result, tool_call_id=call_id, name=SUBAGENT_TOOL)
            return release_wait(message, call_id)

        args = call.get("args") or {}
        name = str(args.get("subagent_type") or "")
        if name not in self._names:
            # ★ 派不出去的当场回成失败结果，不挂起：模型看到后可以改派或自己动手
            allowed = ", ".join(f"`{n}`" for n in self._names)
            return _failed(call_id, f"SubAgent {name!r} 不存在，可用的是：{allowed}")
        try:
            sub_run_id = await self._spawner.spawn(
                name, str(args.get("description") or ""), fresh=bool(args.get("fresh", False))
            )
        except Exception as exc:  # noqa: BLE001
            # ★ 撞上限、同轮同名、会话被占都回成**失败的工具结果**，不冒泡：一次派不出去是
            #   可恢复的局部问题，模型可以改派、合并任务或自己动手；异常会让整个 run 失败
            return _failed(call_id, f"派给 {name} 失败：{exc}")

        # ★ 不写结果：调用留着悬空，只在 waiting_on 里登记。SuspensionMiddleware 在
        #   before_model 看到它就让本段结束（不调模型），子 run 跑完再叫醒这个 run
        return Command(
            update={"waiting_on": {call_id: {"reason": "subagent", "token": str(sub_run_id)}}}
        )


def _failed(call_id: str, text: str) -> ToolMessage:
    return ToolMessage(content=text, tool_call_id=call_id, name=SUBAGENT_TOOL, status="error")
