"""装配中心：`create_deep_agent` —— 把中间件按固定顺序叠成一张图。

filesystem 走 atlas_engine.contracts 的协议（实现方在 server），subagents 与
skills 两条栈是 Atlas 自己的语义 —— 前者让调用方在 SubAgent 自己的会话上起子 run
（本段随即挂起），后者按需投送技能。

★ 中间件的叠加顺序带语义，不能随手调：prompt caching 在调用方中间件之后（改系统
  提示词的中间件放在它后面会让缓存前缀每轮失效）。装不装、TTL 多长由调用方经
  `prompt_cache_ttl` 决定（模型支不支持缓存、用户开没开都只有 server 知道）；
  kernel 只保证装了就排在最内层。完整推导见 doc/detail/kernel.html
  §02。FilesystemMiddleware 只在有文件工具时装（大结果外置已归调用方的上下文管理，
  kernel/context；它不再是恒装的脚手架）。

★ 不按模型做任何调整：系统提示词、工具、中间件都只由调用方决定。原先的 HarnessProfile
  （按模型追加提示词、改写工具描述、剔除工具 / 中间件）2026-10-19 删除。

★ kernel 不装压缩：上下文管理只有调用方的 ContextMiddleware 一套（server 按智能体
  配置装）。原先恒装的 SummarizationMiddleware（引入自上游）2026-10-19 删除 —— 它的
  触发线（窗口 × 0.85）总在 Compaction（有效窗口 × 0.8）之后，只在压缩被关闭、模型
  不在目录里、或 Compaction 熔断时才轮到它；而它的压缩记录只在本轮 graph state 里，
  server 不保存，一旦触发就每轮重压一次。

★ SubAgentMiddleware 排在**调用方中间件之后**：它在 awrap_tool_call 里接管 `task`
  （起子 run、登记 waiting_on），必须在续跑去重（SuspensionMiddleware）和审批
  （task 可以被配成需审批）之内 —— 先去重、先审批，最后才起子 run。
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware.types import (
    AgentMiddleware,
    InputAgentState,
    OutputAgentState,
)
from langchain.agents.structured_output import ResponseFormat
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool
from langgraph.cache.base import BaseCache
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import Checkpointer

from atlas_engine.contracts import FilesystemProtocol, SubAgentSpawner
from atlas_engine.kernel.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from atlas_engine.kernel.middleware.skills import SkillRef, SkillsMiddleware
from atlas_engine.kernel.middleware.subagents import (
    SubAgent,
    SubAgentMiddleware,
)
from atlas_engine.kernel.stack import MiddlewareStack, Slot

logger = logging.getLogger(__name__)

# ★ 图状态就是 langchain 的 AgentState（messages 用标准 add_messages 合并）。原先的
#   DeepAgentState 给 messages 换了 DeltaChannel + 自写 reducer，那是为 checkpoint 存储
#   瘦身（O(N²)→O(N)）；Atlas 不用 checkpointer（历史每轮从 message 表重建），收益为零，
#   却要背一个 beta 通道，2026-10-19 删除。


@dataclass(frozen=True)
class FilesystemConfig:
    """文件系统能力：工作区存储 + 模型可见的文件工具 + 访问规则。

    不要文件能力就不传这个对象（`filesystem=None`），而不是传一个没有
    文件系统的配置 —— 「有工具白名单 / 有权限规则，却没有存储」这种组合
    没有意义，所以 filesystem 必填。
    """

    filesystem: FilesystemProtocol
    """智能体看到的文件系统（Atlas：会话工作区 + 自己的 /skills）。"""

    # ★ Atlas 改动（Sandbox S0）：文件工具白名单。原逻辑默认注册全部工具
    #   （含 execute），造成两个门禁缺口：
    #     · 未勾 filesystem 的 agent 也能 write_file（工具目录说谎）
    #     · execute 在 filesystem 不支持时「注册后运行时报错」—— 模型看得见、
    #       调用后才失败，每次尝试烧一轮 token
    #   传 [] 等于没有文件能力：不装 FilesystemMiddleware。"all" 保持原本的全量行为。
    tools: list[str] | Literal["all"] = "all"
    """可选值见 `FsToolName`；`"all"` 表示全部注册。"""

    permissions: list[FilesystemPermission] | None = None
    """按声明顺序匹配、首条命中生效的访问规则（allow / deny）；都不命中则放行。"""

    def __post_init__(self) -> None:
        if self.filesystem is None:
            msg = "FilesystemConfig 需要 filesystem —— 不要文件能力就传 create_deep_agent(filesystem=None)"
            raise ValueError(msg)


@dataclass(frozen=True)
class SubAgentConfig:
    """SubAgent：`task` 工具认识的名单 + 起子 run 的一方。"""

    agents: Sequence[SubAgent]
    """只有名字、描述与会话模式 —— 写 `task` 的工具描述、校验 subagent_type。为空则不装 `task`。"""

    # ★ Atlas 改动（SubAgent）：`task` 不在图内跑子图，而是让 spawner 在 SubAgent
    #   **自己的会话**上起一个子 run，本段随即挂起（middleware/subagents.py）。
    #   SubAgent 的图**完全不编译**：那份配置会在子 run 里重新装配，
    #   在这里再编译一份等于让「哪份配置生效」有两个答案。
    spawner: SubAgentSpawner

    def __post_init__(self) -> None:
        if self.spawner is None:
            msg = "SubAgentConfig 需要 spawner —— kernel 不在图内编译 SubAgent 的图"
            raise ValueError(msg)


@dataclass(frozen=True)
class GraphOptions:
    """原样透传给 `langchain.agents.create_agent` 的图选项。"""

    response_format: ResponseFormat[Any] | type[Any] | dict[str, Any] | None = None
    """结构化输出格式。"""

    state_schema: type[AgentState] | None = None
    """自定义图状态，须继承 `AgentState`。缺省即 `AgentState`。"""

    context_schema: type[Any] | None = None
    """run 内不可变上下文的 schema。"""

    checkpointer: Checkpointer | None = None
    store: BaseStore | None = None
    debug: bool = False
    name: str | None = None
    """agent 名，同时写进 metadata 的 `lc_agent_name`。"""

    cache: BaseCache | None = None


def create_deep_agent(  # noqa: C901, PLR0912, PLR0915  # Complex graph assembly logic with many conditional branches
    model: BaseChatModel,
    tools: Sequence[BaseTool | Callable | dict[str, Any]] | None = None,
    *,
    system_prompt: str | SystemMessage | None = None,
    middleware: MiddlewareStack | None = None,
    skills: Sequence[SkillRef] | None = None,
    filesystem: FilesystemConfig | None = None,
    subagents: SubAgentConfig | None = None,
    prompt_cache_ttl: Literal["5m", "1h"] | None = None,
    graph: GraphOptions | None = None,
) -> CompiledStateGraph[AgentState[Any], Any, InputAgentState, OutputAgentState[Any]]:  # ty: ignore[invalid-type-arguments]  # ty can't verify generic TypedDicts satisfy StateLike bound
    r"""Create a deep agent.

    Built-in tools, each present only when its capability is configured:

    - `read_file`, `write_file`, `edit_file`, `delete`, `search`: file
        operations (`filesystem=`, narrowed by `FilesystemConfig.tools`)
    - `task`: hand work to a SubAgent (`subagents=`)

    Shell execution (`execute`) is not built in —— the caller installs
    `SandboxMiddleware` via `middleware=`.

    Args:
        model: A constructed `BaseChatModel`. Atlas builds it in
            `providers/llm/factory.py` (gateway endpoint, effort / thinking
            gates); the kernel neither resolves model strings nor falls back
            to a default model.
        tools: Additional tools the agent should have access to.

            These are merged with the built-in tools listed above
            (filesystem tools and `task`).

            Passing tools here is additive — it never removes a built-in.
        system_prompt: The system prompt sent to the model, as given —— nothing
            is appended per model. `None` means an empty authored system prompt.

            Passing a `SystemMessage` preserves any `cache_control` markers
            on its existing content blocks — useful for explicit Anthropic
            prompt-cache breakpoints.
        middleware: The caller's middleware, each in its slot (`MiddlewareStack`):
            suspension, ask-user, permission, context, tools, limits. The order
            comes from the slots only (kernel/stack.py). This function adds its
            own built-ins to their slots —— `SkillsMiddleware` and
            `FilesystemMiddleware` (only with file tools) first in `TOOLS`, `SubAgentMiddleware` in
            `SUBAGENTS` —— and appends
            [`AnthropicPromptCachingMiddleware`][langchain_anthropic.middleware.AnthropicPromptCachingMiddleware]
            innermost when `prompt_cache_ttl` is set (no-ops for non-Anthropic models).

            To run without the `task` tool, pass no subagents via
            `subagents=`.
        skills: Skills already delivered to the workspace, each a `SkillRef`
            (`name` + `description` + `path` of its SKILL.md).

            Installs `SkillsMiddleware`, which puts the skill index
            (name + description) into the system prompt; the model reads
            the full SKILL.md at `path` with `read_file` when it decides a
            skill applies.
        filesystem: The agent's filesystem, visible file tools and access rules
            —— see `FilesystemConfig`. `None` (or an empty tool list) means
            **no file capability**: `FilesystemMiddleware` is not installed.

            `FilesystemConfig.permissions` are evaluated in declaration
            order; the first match wins and no match means allow. Each
            rule's `mode` is `"allow"` (default) or `"deny"` (the tool returns
            a permission-denied error). Rules apply at the
            tool level for the built-in filesystem tools, not at the filesystem
            level. They do not reach subagents —— those run in their own
            child runs with their own assembly.
        subagents: SubAgents the agent can hand work to via the `task`
            tool —— see `SubAgentConfig`. A `task` call never runs in-graph:
            `SubAgentConfig.spawner` starts a child run in the SubAgent's own
            session and the segment suspends until it finishes.
            `None` or an empty `agents` means no `task` tool.
        prompt_cache_ttl: Prompt-cache TTL (`"5m"` / `"1h"`) for Anthropic
            models. `None` means no caching middleware —— the caller decides,
            since only it knows whether the model supports caching and whether
            the user turned it on. When set, the caching middleware is the
            innermost wrapper so it tags the final request (system prompt,
            last tool, message tail).
        graph: Options passed through to
            [`create_agent`][langchain.agents.create_agent] —— see
            `GraphOptions`. `state_schema`, when given, is used as the base
            graph schema and merged with state schemas contributed by
            middleware; prefer extending state through middleware so the
            extra fields stay scoped to the hooks and tools that use them.

            ```python
            from langchain.agents import AgentState

            from atlas_engine.kernel.graph import GraphOptions


            class MyState(AgentState):
                page_url: str
                file_urls: list[str]


            agent = create_deep_agent(model=..., graph=GraphOptions(state_schema=MyState))
            ```

    Returns:
        A configured deep agent.
    """
    # ★ 没有 filesystem 配置就**没有文件能力** —— 不再回落到内存里的临时存储。
    #   回落意味着模型以为自己有持久工作区，写进去的东西 run 结束即弃，
    #   而它不会收到任何提示。宁可结构性地没有这些工具。
    #   白名单为空同理：不装 FilesystemMiddleware，模型也看不到它的说明。

    # ★ Atlas：SubAgent 只有**一种**形态 —— 名字 + 描述，`task` 经注入的 spawner
    #   在 SubAgent 自己的会话上起子 run。图内编译子图、CompiledSubAgent、
    #   AsyncSubAgent、自动补 general-purpose 已整体删除：多一种执行形态就多一套
    #   要维护的治理语义（审批 / 步数 / 白名单各一份），而那些守卫在「子 run 是
    #   普通 run」的模型下是免费继承的。「有名单没 spawner」由 SubAgentConfig 构造时拒绝。
    subagent_middleware: list[AgentMiddleware[Any, Any, Any]] = []
    if subagents is not None and subagents.agents:
        subagent_middleware.append(
            SubAgentMiddleware(subagents=list(subagents.agents), spawner=subagents.spawner)
        )
    options = graph if graph is not None else GraphOptions()

    # ★ 中间件顺序只由槽位决定（kernel/stack.py）。本函数的内置件进各自的槽位：
    #   技能、文件排在 TOOLS 的最前（技能清单的说明在文件工具之前）。
    stack = MiddlewareStack()
    if skills:
        stack.add(Slot.TOOLS, SkillsMiddleware(skills=skills))
    if filesystem is not None and filesystem.tools:
        stack.add(
            Slot.TOOLS,
            FilesystemMiddleware(
                filesystem=filesystem.filesystem,
                _permissions=filesystem.permissions,
                tools=filesystem.tools,
            ),
        )
    if middleware is not None:
        stack.merge(middleware)
    stack.extend(Slot.SUBAGENTS, subagent_middleware)
    deepagent_middleware: list[AgentMiddleware[Any, Any, Any]] = stack.ordered()
    if prompt_cache_ttl is not None:
        # ★ 不是 ChatAnthropic 时它什么也不做（ignore）。「这个模型支不支持缓存」由调用方
        #   按实测能力判断 —— 不支持就别传 TTL，而不是靠这里的类型探测兜底。
        # ★ 永远在最内层：改系统提示词的中间件都在它外面，缓存前缀才不会每轮失效。
        deepagent_middleware.append(
            AnthropicPromptCachingMiddleware(ttl=prompt_cache_ttl, unsupported_model_behavior="ignore")
        )
    # ★ 不装 HumanInTheLoopMiddleware：它靠 LangGraph 的 interrupt() + checkpointer，
    #   而 Atlas 不用 checkpointer —— 人工审批走调用方的 PermissionMiddleware（挂起）。

    # ★ 系统提示词就是调用方给的那一份，不按模型追加任何东西。原先的 HarnessProfile
    #   会给特定模型（如 claude-haiku-4-5）悄悄补一段提示词，用户在配置里看不到，
    #   2026-10-19 删除。没给时传空串，与删除前一致。
    final_system_prompt: str | SystemMessage = system_prompt if system_prompt is not None else ""

    return create_agent(
        model,
        system_prompt=final_system_prompt,
        tools=tools,
        middleware=deepagent_middleware,
        response_format=options.response_format,
        context_schema=options.context_schema,
        checkpointer=options.checkpointer,
        store=options.store,
        debug=options.debug,
        name=options.name,
        cache=options.cache,
        state_schema=options.state_schema,
    ).with_config(
        {
            "recursion_limit": 9_999,
            "metadata": {
                "ls_integration": "deepagents",
                "lc_agent_name": options.name,
            },
        }
    )
