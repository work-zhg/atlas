"""装配中心：`create_deep_agent` —— 把中间件按固定顺序叠成一张图。

backend 走 atlas_engine.contracts 的协议（实现方在 server），subagents 与
skills 两条栈是 Atlas 自己的语义 —— 前者把委派交还调用方、在子智能体自己的
会话上起子 run，后者按需投送技能。

★ 中间件的叠加顺序带语义，不能随手调：profile 中间件必须在 prompt caching
  之前（它会改系统提示词，放后面会让缓存前缀每轮失效）；_ToolExclusion 必须
  最后跑（等所有注入工具的中间件跑完才剔得干净）。完整推导见
  doc/detail/kernel.html §02。FilesystemMiddleware 与 SubAgentMiddleware 是
  结构上不可剔除的脚手架，试图排除它们直接 ValueError 而不是退化运行。
"""

import logging
from collections.abc import Callable, Sequence
from typing import Annotated, Any, Literal, Required

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, InterruptOnConfig
from langchain.agents.middleware.types import (
    AgentMiddleware,
    InputAgentState,
    OutputAgentState,
    ResponseT,
    StateT_co,
)
from langchain.agents.structured_output import ResponseFormat
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.cache.base import BaseCache
from langgraph.channels.delta import DeltaChannel
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import Checkpointer
from langgraph.typing import ContextT

from atlas_engine.kernel._excluded_middleware import (
    _apply_excluded_middleware,
    _validate_excluded_middleware_config,
    _verify_excluded_middleware_coverage,
)
from atlas_engine.kernel._messages_reducer import _messages_delta_reducer
from atlas_engine.kernel._models import resolve_model
from atlas_engine.kernel._tools import _apply_tool_description_overrides
from atlas_engine.contracts import DelegationProtocol, FilesystemProtocol
from atlas_engine.kernel.middleware._fs_interrupt import _build_interrupt_on_from_permissions
from atlas_engine.kernel.middleware._prompt_caching import append_prompt_caching_middleware
from atlas_engine.kernel.middleware._tool_exclusion import _ToolExclusionMiddleware
from atlas_engine.kernel.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from atlas_engine.kernel.middleware.patch_tool_calls import PatchToolCallsMiddleware
from atlas_engine.kernel.middleware.skills import SkillRef, SkillsMiddleware
from atlas_engine.kernel.middleware.subagents import (
    SubAgent,
    SubAgentMiddleware,
)
from atlas_engine.kernel.middleware.summarization import create_summarization_middleware
from atlas_engine.kernel.profiles.harness.harness_profiles import (
    _apply_profile_prompt,
    _harness_profile_for_model,
)

logger = logging.getLogger(__name__)


class DeepAgentState(AgentState):
    """AgentState with `DeltaChannel` on messages to reduce checkpoint growth from O(N²) to O(N)."""

    messages: Required[Annotated[list[AnyMessage], DeltaChannel(_messages_delta_reducer, snapshot_frequency=50)]]  # ty: ignore[invalid-argument-type]


def _merge_fs_interrupt_on(
    fs_interrupt_on: dict[str, InterruptOnConfig],
    user_interrupt_on: dict[str, bool | InterruptOnConfig] | None,
) -> dict[str, bool | InterruptOnConfig] | None:
    """Combine filesystem-permission configs with user-defined interrupts.

    User-defined `interrupt_on` entries take precedence over generated
    filesystem-permission entries with the same tool name. Returns `None` when
    there are no interrupts to configure, allowing `HumanInTheLoopMiddleware` to
    be omitted.
    """
    if not fs_interrupt_on and not user_interrupt_on:
        return None
    merged: dict[str, bool | InterruptOnConfig] = {**fs_interrupt_on}
    if user_interrupt_on:
        merged.update(user_interrupt_on)
    return merged


def _apply_custom_middleware(
    base: list[AgentMiddleware[Any, Any, Any]],
    custom: Sequence[AgentMiddleware[Any, Any, Any]],
    *,
    core_names: set[str] | None = None,
) -> list[AgentMiddleware[Any, Any, Any]]:
    """Merge custom middleware into the base stack by name.

    - If its `.name` matches a name still present in `base`: replace in-place,
      preserving stack order.
    - Otherwise: a brand-new entry lands after the last `core_names` member (so it
      precedes the profile/prompt-caching tail), or at the end when
      `core_names` is unset.
    """
    if not custom:
        return list(base)
    current_names = {m.name for m in base}
    replacements: dict[str, AgentMiddleware[Any, Any, Any]] = {}
    to_append: list[AgentMiddleware[Any, Any, Any]] = []
    for m in custom:
        if m.name in current_names:
            replacements[m.name] = m
        else:
            to_append.append(m)
    result = list(base)
    for i, m in enumerate(result):
        if m.name in replacements:
            result[i] = replacements[m.name]
    if to_append and core_names is not None:
        # Land new middleware after the last core entry, ahead of the tail.
        pos = max((i for i, m in enumerate(result) if m.name in core_names), default=len(result) - 1) + 1
        result[pos:pos] = to_append
    else:
        result.extend(to_append)
    return result


_REQUIRED_MIDDLEWARE: tuple[tuple[type[AgentMiddleware[Any, Any, Any]], tuple[str, ...]], ...] = (
    (FilesystemMiddleware, ()),
    (SubAgentMiddleware, ()),
)
"""Scaffolding middleware that core deep agent features depend on.

Each entry pairs a class with any extra string aliases its `.name` may take
beyond `__name__`. Removing any of these silently breaks core features:
`FilesystemMiddleware` backs every built-in file tool and now also enforces
`permissions` rules (a security guarantee), while `SubAgentMiddleware` backs
the `task` tool handler.

Tracked here so `HarnessProfile.excluded_middleware` cannot strip them:
`_apply_excluded_middleware` raises `ValueError` rather than proceeding with
a silently degraded agent.
"""

_REQUIRED_MIDDLEWARE_CLASSES: frozenset[type[AgentMiddleware[Any, Any, Any]]] = frozenset(cls for cls, _ in _REQUIRED_MIDDLEWARE)
"""Set of all class types that cannot be excluded from the middleware stack.

Derived from `_REQUIRED_MIDDLEWARE` and used for quick membership testing.
"""

_REQUIRED_MIDDLEWARE_NAMES: frozenset[str] = frozenset(name for cls, aliases in _REQUIRED_MIDDLEWARE for name in (cls.__name__, *aliases))
"""Set of all `.name` values that cannot be excluded from the middleware stack.

Derived from `_REQUIRED_MIDDLEWARE` and used for quick membership testing.
"""


def create_deep_agent(  # noqa: C901, PLR0912, PLR0915  # Complex graph assembly logic with many conditional branches
    model: str | BaseChatModel | None = None,
    tools: Sequence[BaseTool | Callable | dict[str, Any]] | None = None,
    *,
    system_prompt: str | SystemMessage | None = None,
    middleware: Sequence[AgentMiddleware[StateT_co, ContextT]] = (),
    subagents: Sequence[SubAgent] | None = None,
    skills: Sequence[SkillRef] | None = None,
    permissions: list[FilesystemPermission] | None = None,
    backend: FilesystemProtocol | None = None,
    # ★ Atlas 改动（Sandbox S0）：文件工具白名单，贯通主 agent / 子智能体默认栈 /
    #   general-purpose 三处 FilesystemMiddleware。原逻辑三处都默认注册全部工具
    #   （含 execute），造成两个门禁缺口：
    #     · 未勾 filesystem 的 agent 也能 write_file（工具目录说谎）
    #     · execute 在 backend 不支持时「注册后运行时报错」—— 模型看得见、
    #       调用后才失败，每次尝试烧一轮 token
    #   传 [] 时中间件本体保留（它是 _REQUIRED_MIDDLEWARE 脚手架：大结果外置、
    #   权限机制挂在上面），但模型看不到任何文件工具。"all" 保持原本的全量行为。
    filesystem_tools: list[str] | Literal["all"] = "all",
    # ★ Atlas 改动（Subagent）：注入后 `task` 不再在图内跑子图，而是把
    #   (任务书, 名字, fresh) 交给调用方 —— 由它在子智能体**自己的会话**上
    #   起一个子 run。见 middleware/subagents.py::Delegate。
    #   非 None 时子智能体图**完全不编译**：那份配置会在子 run 里重新装配，
    #   在这里再编译一份等于让「哪份配置生效」有两个答案。
    subagent_delegate: DelegationProtocol | None = None,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    response_format: ResponseFormat[ResponseT] | type[ResponseT] | dict[str, Any] | None = None,
    state_schema: type[DeepAgentState] | None = None,
    context_schema: type[ContextT] | None = None,
    checkpointer: Checkpointer | None = None,
    store: BaseStore | None = None,
    debug: bool = False,
    name: str | None = None,
    cache: BaseCache | None = None,
) -> CompiledStateGraph[AgentState[ResponseT], ContextT, InputAgentState, OutputAgentState[ResponseT]]:  # ty: ignore[invalid-type-arguments]  # ty can't verify generic TypedDicts satisfy StateLike bound
    r"""Create a deep agent.

    By default, this agent has access to the following tools:

    - `ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`: file operations
    - `execute`: run shell commands
    - `task`: call subagents

    The `execute` tool allows running shell commands if the backend implements
    [`SandboxProtocol`][SandboxProtocol].
    For non-sandbox backends, the `execute` tool will return an error message.

    Args:
        model: The model to use. **Required** — passing `None` raises
            `ValueError`.

            The default-model fallback was removed: silently falling back to a
            hardcoded model contradicts the no-silent-degradation rule (§13.2).
            The parameter keeps its `| None` type only so the error can be
            raised with a clear message instead of a `TypeError`.

            Accepts a `provider:model` string (e.g., `openai:gpt-5.5`); see
            [`init_chat_model`][langchain.chat_models.init_chat_model(model_provider)]
            for supported values. You can also pass a pre-initialized
            [`BaseChatModel`][langchain.chat_models.BaseChatModel] instance directly.

            !!! note "OpenAI Models and Data Retention"

                If an `openai:` model is used, the agent will use the OpenAI
                Responses API by default. To use OpenAI chat completions
                instead, initialize the model with
                `init_chat_model("openai:...", use_responses_api=False)` and
                pass the initialized model instance here.

                To disable data retention with the Responses API, use
                `init_chat_model("openai:...", use_responses_api=True, store=False, include=["reasoning.encrypted_content"])`
                and pass the initialized model instance here.
        tools: Additional tools the agent should have access to.

            These are merged with the built-in tool suite listed above
            (filesystem tools, `execute`, and `task`).

            Passing tools here is additive — it never removes a built-in.
            To drop a built-in tool, register a
            `HarnessProfile` with
            `excluded_tools`.
        system_prompt: Caller-authored system instructions (`USER`) placed
            first in the system prompt sent to the model.

            The final authored prompt is assembled as `USER` -> `BASE` -> `SUFFIX`.
            `BASE` is empty unless the active
            `HarnessProfile` defines
            `base_system_prompt`, and `SUFFIX` is the profile's optional
            `system_prompt_suffix`. Parts are separated by blank lines.

            With `system_prompt=None` and no profile `base_system_prompt` or
            `system_prompt_suffix`, the model receives an empty authored system
            prompt.

            Passing a `SystemMessage` preserves any `cache_control` markers
            on its existing content blocks — useful for explicit Anthropic
            prompt-cache breakpoints. When profile content is present, its
            assembled `BASE` and `SUFFIX` are appended as an additional text
            content block after the caller's blocks.

            See Prompt assembly
            for the full case-by-case breakdown.
        middleware: Additional middleware to apply after the base stack
            but before the tail middleware. The full ordering is:

            Base stack:

            - `SkillsMiddleware` (if `skills` is provided)
            - `FilesystemMiddleware`
            - `SubAgentMiddleware`
                (if `subagents` are provided — requires `subagent_delegate`)
            - [`SummarizationMiddleware`][langchain.agents.middleware.SummarizationMiddleware]
            - `PatchToolCallsMiddleware`

            *User middleware is inserted here.*

            Tail stack:

            - Harness profile `extra_middleware` (if any)
            - `_ToolExclusionMiddleware` (if profile has `excluded_tools`)
            - [`AnthropicPromptCachingMiddleware`][langchain_anthropic.middleware.AnthropicPromptCachingMiddleware] (unconditional; no-ops for
                non-Anthropic models)
            - [`BedrockPromptCachingMiddleware`](https://reference.langchain.com/python/langchain-aws/middleware/prompt_caching/BedrockPromptCachingMiddleware)
                when `langchain-aws` is installed (no-ops for non-Bedrock models)
            - [`FireworksPromptCachingMiddleware`](https://reference.langchain.com/python/integrations/langchain_fireworks/middleware/prompt_caching/FireworksPromptCachingMiddleware)
                when `langchain-fireworks` is installed (no-ops for non-Fireworks models)
            - [`HumanInTheLoopMiddleware`][langchain.agents.middleware.HumanInTheLoopMiddleware] (if `interrupt_on` is provided)

            After assembly, any entries in the profile's
            `excluded_middleware` are filtered from the final stack. Class
            entries match exact type; string entries match
            `AgentMiddleware.name` exactly (e.g. `"SummarizationMiddleware"`
            drops the summarization middleware via its public alias).
            Entries that match nothing in the assembled stack raise
            `ValueError`, as does excluding any class in the harness's
            protected scaffolding set (e.g.,
            `FilesystemMiddleware`
            or `SubAgentMiddleware`).

            To run without the `task` tool, pass no subagents via
            `subagents=`.
        subagents: Subagent specs available to the main agent via the `task`
            tool. **Delegation is the only form** —— each entry is a
            `SubAgent`
            (`name` + `description`); execution happens in the subagent's
            own child run via `subagent_delegate`, never as an in-graph
            subgraph. Passing `subagents` without `subagent_delegate`
            raises `ValueError`.

        skills: List of skill source paths (e.g., `["/skills/user/", "/skills/project/"]`).

            Paths must be specified using POSIX conventions (forward slashes)
            and are relative to the backend's root. When using
            `StateBackend` (default), provide skill files via
            `invoke(files={...})`. With `FilesystemBackend`, skills are loaded
            from disk relative to the backend's `root_dir`. Later sources
            override earlier ones for skills with the same name (last one wins).

            Display names are automatically derived from paths.

            Memory is loaded at agent startup and added into the system prompt.
        permissions: List of `FilesystemPermission` rules for the main agent
            and its subagents.

            Rules are evaluated in declaration order; the first match wins.
            If no rule matches, the call is allowed.

            Each rule's `mode` can be:

            - `"allow"` (default): the call proceeds.
            - `"deny"`: the tool returns a permission-denied error.
            - `"interrupt"`: the call pauses for human approval via
                `HumanInTheLoopMiddleware`. A `HumanInTheLoopMiddleware` is
                auto-installed when any interrupt-mode rule is present, and the
                generated `interrupt_on` entries are merged with the
                `interrupt_on` argument below (user-supplied entries win per
                tool name). Requires a `langchain` version that supports the
                `when` predicate on `InterruptOnConfig`.

            Subagents inherit these rules unless they specify their own
            `permissions` field, which replaces the parent's rules entirely.

            `FilesystemMiddleware` applies these permissions at the tool
            level for its built-in filesystem tools, not at the backend
            level. Direct backend usage does not currently incorporate
            `permissions`.
        backend: Optional backend for file storage and execution.

            Pass a `Backend` instance (e.g. `StateBackend()`).

            For execution support, use a backend that
            implements [`SandboxProtocol`][SandboxProtocol].
        interrupt_on: Mapping of tool names to interrupt configs.

            Pass to pause agent execution at specified tool calls for human
            approval or modification.

            This config always applies to the main agent. Subagents run in
            their own child runs — approval and interrupt behavior there
            comes from the child run's own assembly, not from this config.

            For example, `interrupt_on={"edit_file": True}` pauses before
            every edit.
        response_format: A structured output response format to use for the agent.
        state_schema: Custom state schema for the agent graph. Must be a
            `TypedDict` subclass of
            `DeepAgentState` so the
            built-in `DeltaChannel` reducer on `messages` is preserved.

            Generally, prefer defining state extensions with middleware so
            the extra fields stay scoped to the hooks and tools that use
            them.

            When provided, this schema is used as the base graph schema and
            is merged with state schemas contributed by middleware.
            Subagents do not see it — they run in their own child runs
            with their own graphs.

            ```python
            from atlas_engine.kernel.graph import DeepAgentState


            class MyState(DeepAgentState):
                page_url: str
                file_urls: list[str]


            agent = create_deep_agent(model=..., state_schema=MyState)
            ```
        context_schema: Schema class that defines immutable run-scoped context.

            Passed through to [`create_agent`][langchain.agents.create_agent].
        checkpointer: Optional `Checkpointer` for persisting agent state
            between runs.

            Passed through to [`create_agent`][langchain.agents.create_agent].
        store: Optional store for persistent storage (required if backend
            uses `StoreBackend`).

            Passed through to [`create_agent`][langchain.agents.create_agent].
        debug: Whether to enable debug mode.

            Passed through to [`create_agent`][langchain.agents.create_agent].
        name: The name of the agent.

            Passed through to [`create_agent`][langchain.agents.create_agent].
        cache: The cache to use for the agent.

            Passed through to [`create_agent`][langchain.agents.create_agent].

    Returns:
        A configured deep agent.

    Raises:
        ImportError: If a required provider package is missing or below the
            minimum supported version (e.g., `langchain-openrouter`).
        ValueError: If the active `HarnessProfile.excluded_middleware`
            references a class in the harness's protected scaffolding set
            (e.g.,
            `FilesystemMiddleware`
            or
            `SubAgentMiddleware`),
            uses a private (underscore-prefixed) name, collides with multiple
            distinct middleware classes, or matches no entry in the assembled
            stack.
    """
    # `DeepAgentState` is a `TypedDict`; TypedDicts disallow `issubclass`, so the
    # subclass constraint on `state_schema` is enforced by typing alone and not
    # validated at runtime.

    _model_spec: str | None = model if isinstance(model, str) else None

    # ★ model 必填。原本的 model=None → 默认 ChatAnthropic 回落
    #   连同整个 _api 弃用告警机制一起删除：kernel 没有外部用户，弃用期
    #   服务的对象不存在，静默回落一个硬编码模型则与 §13.2 相悖。
    if model is None:
        msg = "create_deep_agent 需要显式的 model —— 默认模型回落已删除"
        raise ValueError(msg)
    model = resolve_model(model)
    _profile = _harness_profile_for_model(model, _model_spec)
    # Validate profile-level invariants (required scaffolding, private names)
    _validate_excluded_middleware_config(
        _profile,
        required_classes=_REQUIRED_MIDDLEWARE_CLASSES,
        required_names=_REQUIRED_MIDDLEWARE_NAMES,
    )
    # Accumulate which entries matched across the main agent + general-purpose
    # subagent stacks (both use `_profile`). A profile-level entry only has to
    # match somewhere, not in every stack, so coverage is verified once after
    # all filters have run.
    _main_matched_classes: set[type[AgentMiddleware[Any, Any, Any]]] = set()
    _main_matched_names: set[str] = set()

    # Copy of `tools` with any harness-specific description rewrites.
    # (Tool exclusion is handled by _ToolExclusionMiddleware which filters
    # all tools (user-supplied and middleware-injected) in one place.)
    _tools = _apply_tool_description_overrides(
        tools,
        _profile.tool_description_overrides,
    )

    # ★ 没有 filesystem 配置就**没有文件能力** —— 不再回落 StateBackend。
    #   回落意味着模型以为自己有持久工作区，写进去的东西 run 结束即弃，
    #   而它不会收到任何提示。宁可结构性地没有这些工具。
    #   FilesystemMiddleware 本体仍会装（它是脚手架），但 tools 为空、
    #   大结果外置一并关闭 —— 外置出去的内容没有 read_file 读得回来。

    # The built-in tool-usage guidance prose duplicates the tools' own schema
    # descriptions, so the kernel-owned middleware (filesystem / subagent /
    # async-subagent) default to emitting none of it; only the essential dynamic
    # bits remain (filesystem's host-path routing, empty for non-composite
    # backends; the available-agent list, which reaches the model through the
    # `task` tool / async tools). `TodoListMiddleware` is from langchain and
    # defaults to its full prompt, so it is the one middleware passed
    # `system_prompt=""` here to trim it. Skills keeps its fragment: it is the
    # only channel that surfaces the loaded skill index, and it is built only
    # when the caller passes `skills=`.

    # ★ Atlas：子智能体只有**一种**形态 —— 名字 + 描述，经 `task` 委派给
    #   注入的受理方，在子智能体自己的会话上起子 run。图内编译子图、
    #   CompiledSubAgent、AsyncSubAgent、自动补 general-purpose 已整体删除：
    #   多一种执行形态就多一套要维护的治理语义（审批 / 步数 / 白名单各一份），
    #   而那些守卫在「子 run 是普通 run」的模型下是免费继承的。
    if subagents and subagent_delegate is None:
        msg = "subagents 需要 subagent_delegate —— kernel 不再图内编译子智能体图"
        raise ValueError(msg)
    inline_subagents: list[SubAgent] = list(subagents or [])

    # Build main agent middleware stack
    deepagent_middleware: list[AgentMiddleware[Any, Any, Any]] = []
    if skills:
        deepagent_middleware.append(SkillsMiddleware(skills=skills))
    deepagent_middleware.append(
        FilesystemMiddleware(
            backend=backend,
            custom_tool_descriptions=_profile.tool_description_overrides,
            _permissions=permissions,
            tools=filesystem_tools,
        )
    )
    if inline_subagents:
        deepagent_middleware.append(
            SubAgentMiddleware(
                subagents=inline_subagents,
                # Overrides the task tool description. Value should include
                # {available_agents} — a format placeholder replaced with the
                # subagent name/description list. Without it the model can't
                # see which subagents exist. None (default) uses the built-in
                # template. Stale keys silently no-op if the tool is renamed.
                task_description=_profile.tool_description_overrides.get("task"),
                delegate=subagent_delegate,
            )
        )
    deepagent_middleware.extend(
        [
            create_summarization_middleware(model, backend),
            PatchToolCallsMiddleware(),
        ]
    )

    # Names of the core stack, captured before the tail is appended so new user
    # middleware can splice in ahead of the profile/prompt-caching tail.
    _main_core_names = {m.name for m in deepagent_middleware}
    deepagent_middleware.extend(_profile.materialize_extra_middleware())
    append_prompt_caching_middleware(deepagent_middleware)
    main_interrupt_on = _merge_fs_interrupt_on(
        _build_interrupt_on_from_permissions(permissions or []),
        interrupt_on,
    )
    if main_interrupt_on is not None:
        deepagent_middleware.append(HumanInTheLoopMiddleware(interrupt_on=main_interrupt_on))
    deepagent_middleware = _apply_excluded_middleware(
        deepagent_middleware,
        _profile,
        matched_classes=_main_matched_classes,
        matched_names=_main_matched_names,
    )
    deepagent_middleware = _apply_custom_middleware(deepagent_middleware, middleware or [], core_names=_main_core_names)
    deepagent_middleware = _apply_excluded_middleware(
        deepagent_middleware,
        _profile,
        matched_classes=_main_matched_classes,
        matched_names=_main_matched_names,
    )
    # Tool exclusion runs after custom middleware so excluded tool names are
    # stripped last and cannot be restored by a custom wrap_model_call.
    if _profile.excluded_tools:
        deepagent_middleware.append(_ToolExclusionMiddleware(excluded=_profile.excluded_tools))
    # Verify every main-profile exclusion matched at least one middleware in
    # either the main agent stack or the GP subagent stack. An entry that
    # matched nothing across both is almost certainly a typo or a stale
    # profile.
    _verify_excluded_middleware_coverage(
        _profile,
        _main_matched_classes,
        _main_matched_names,
        required_classes=_REQUIRED_MIDDLEWARE_CLASSES,
        required_names=_REQUIRED_MIDDLEWARE_NAMES,
    )

    base_prompt = _apply_profile_prompt(_profile, "")
    if system_prompt is None:
        final_system_prompt: str | SystemMessage = base_prompt
    elif isinstance(system_prompt, SystemMessage):
        if base_prompt:
            final_system_prompt = SystemMessage(content_blocks=[*system_prompt.content_blocks, {"type": "text", "text": f"\n\n{base_prompt}"}])
        else:
            final_system_prompt = system_prompt
    else:
        final_system_prompt = system_prompt + (f"\n\n{base_prompt}" if base_prompt else "")

    return create_agent(
        model,
        system_prompt=final_system_prompt,
        tools=_tools,
        middleware=deepagent_middleware,
        response_format=response_format,
        context_schema=context_schema,
        checkpointer=checkpointer,
        store=store,
        debug=debug,
        name=name,
        cache=cache,
        state_schema=state_schema if state_schema is not None else DeepAgentState,
    ).with_config(
        {
            "recursion_limit": 9_999,
            "metadata": {
                "ls_integration": "deepagents",
                "lc_agent_name": name,
            },
        }
    )
