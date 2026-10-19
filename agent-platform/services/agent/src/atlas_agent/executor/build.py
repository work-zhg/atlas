"""spec + 能力对象 → agent 图。装配策略的唯一住所。

前身是 engine 的 `agent.py`（防腐层）。kernel 永久内化之后那层防腐失去了
对象，装配策略随之收进 server —— **知道配置、知道能力从哪来的层才有资格
决定装什么、什么顺序**。engine 只剩词汇（spec / events）与执行循环（runner）。

★ 本模块刻意保持纯净：只 import engine / kernel，不碰 DB、Redis、Settings。
  测试用假模型 + 假能力对象即可脱离一切基础设施装配出真实的图 ——
  IO 的收集在 assembly.py，那里才认识 boto3 与 Redis。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from atlas_engine.contracts import InvalidSpec
from atlas_engine.kernel.middleware.approval import ApprovalMiddleware
from atlas_engine.kernel.middleware.limits import StepLimitMiddleware, ToolGovernorMiddleware
from atlas_engine.kernel.middleware.suspension import SuspensionMiddleware

from atlas_agent.domain.mcp_naming import approval_target
from atlas_agent.domain.tool_registry import FILESYSTEM_TOOL, filesystem_tools_for, middleware_for

if TYPE_CHECKING:
    from atlas_engine.contracts import (
        ApprovalGate,
        FilesystemProtocol,
        SandboxProtocol,
        SkillRef,
        SubAgentSpawner,
    )
    from langchain_core.language_models import BaseChatModel

    from atlas_agent.domain.spec import AgentSpec

__all__ = ["build_graph"]

#: 文件工具那一段。它与沙箱无关 —— 没配 bash 的 agent 也有工作区。
_FILESYSTEM_HINT = (
    "文件工具作用于会话工作区，内容跨轮保留。"
    "search 只匹配路径与文件名，**不搜索文件内容** —— 要按内容查找，用 execute 跑 grep。"
)

#: 沙箱那一段（doc/native-sandbox-design.html）。
#:
#: ★ /workspace 与文件工具是**同一份数据**（对象存储的 FUSE 挂载 + 每次执行前后的一致性屏障），
#:   所以 write_file 之后可以直接执行。本地盘（HOME、/repos、/tmp）沙箱重建后清空 —— 必须明写，
#:   否则模型会把依赖装进 HOME、下一次闲置回收后又当它还在。
_SANDBOX_HINT = (
    "execute 在本会话专属的 Linux 沙箱里执行命令。工作目录 /workspace 就是文件工具的工作区"
    "（同一份数据：write_file 之后可以直接执行，命令写的文件 read_file 也读得到）。"
    "\n其它可写目录：HOME（/home/agent）、/repos、/tmp 是本地盘，速度快，但沙箱重建后清空 —— "
    "git 仓库与依赖安装放 /repos 或 HOME，需要保留的产物写回 /workspace。"
    "\n首次使用要启动沙箱，会慢几秒。命令没有时长上限；不要运行等待交互输入的命令。"
    "后台服务用 `nohup cmd > /tmp/x.log 2>&1 &` 启动，它在本会话内持续存在。"
)


def build_graph(
    spec: AgentSpec,
    model: BaseChatModel,
    *,
    filesystem: FilesystemProtocol | None = None,
    sandbox: SandboxProtocol | None = None,
    approvals: ApprovalGate | None = None,
    compactor: Any = None,
    skills: list[SkillRef] | None = None,
    extra_tools: list[Any] | None = None,
    spawner: SubAgentSpawner | None = None,
    instructions: str | None = None,
    deferred_tools: list[Any] | None = None,
    prompt_cache_ttl: Literal["5m", "1h"] | None = None,
) -> Any:
    """按 spec 组装 agent 图。返回 CompiledStateGraph，交给 runner.run 驱动。

    能力对象一律可缺省 —— None 的含义是**该能力结构性不存在**（对应工具
    不注册），不是"注册后调用才报错"：后者模型看得见、每次尝试烧一轮 token。

    prompt_cache_ttl 为 None 时不打缓存标记。是否传由 assembly 按「spec 开了缓存 且
    模型目录实测支持」决定 —— 本模块不碰 DB 与 Settings。
    """
    from atlas_engine.kernel.graph import FilesystemConfig, SubAgentConfig, create_deep_agent

    system_prompt = spec.system_prompt or ""
    if FILESYSTEM_TOOL in spec.tool_names:
        system_prompt = f"{system_prompt}\n\n{_FILESYSTEM_HINT}".strip()
    if sandbox is not None:
        system_prompt = f"{system_prompt}\n\n{_SANDBOX_HINT}".strip()
    # 持久指令（上下文分层 L1）：平台 → 用户 → 工作区，排在系统提示词与工具说明之后
    if instructions:
        system_prompt = f"{system_prompt}\n\n{instructions}".strip()

    # ★ SubAgent 不设层数：native SubAgent 的 spec 带着同一份名录（spec_for_subagent），
    #   它的子 run 在这里同样装上 task。跑飞的递归由整棵树的累计上限兜住（SubAgentService）。
    subagent_config: SubAgentConfig | None = None
    if spec.subagents:
        if spawner is None:
            # §13.2 不允许静默降级：配了 SubAgent 却没注入 spawner，砍掉 task
            # 会让模型按「没有帮手」的方式做事，而配置界面显示它有 —— 报错。
            raise InvalidSpec(
                "配置了 SubAgent 但未注入 SubAgentSpawner；"
                "assembly 注入 SubAgentService，单测注入实现该协议的假对象",
                subagents=[sub.name for sub in spec.subagents],
            )
        # 只交 name / description / session_mode 给 task 的工具描述 —— SubAgent 的模型、
        # 中间件、权限都在**子 run** 里按 spec_for_subagent 派生的 AgentSpec
        # 重新装配（detail/subagent.html §09）。
        subagent_config = SubAgentConfig(
            agents=[
                {
                    "name": sub.name,
                    "description": sub.description,
                    "system_prompt": sub.system_prompt,
                    # 工具描述按模式说明「记不记得 / 能不能同轮并行」
                    "session_mode": sub.session_mode,
                }
                for sub in spec.subagents
            ],
            spawner=spawner,
        )

    # ★ 中间件顺序即语义，从外到内分两层显式列出（列表顺序 = 包裹顺序）：
    #
    #   外层（顺序敏感）：
    #     1. suspension 必须最外 —— 它的 before_model 要在任何「为模型调用做
    #        准备」的工作之前跳出。排在 compactor 后面的话，一个马上就要挂起
    #        的段可能先触发一次压缩：调一次 LLM、写一次 thread.summary，
    #        然后才发现这一段根本不调模型。白花钱且把摘要边界搞乱。
    #     2. approval  次之 —— 排后面的话工具已被内层执行掉，再问也来不及
    #     3. compactor 再次之 —— abefore_model 要在其它中间件动手前拿到完整消息列表
    #   内层（彼此无序）：
    #     工具中间件 + 步数上限 + 工具并发/熔断（§4.4 / §13.2）
    #
    # 新增有序中间件时把它插进 outer 的正确位置并写明理由，不要用 insert(0)。
    outer: list[Any] = []
    # ★ 无条件装：它无状态、零开销（只扫最后几条消息），而「悬空的 tool_call
    #   续跑时自愈」这条判据对任何 agent 都有价值 —— 不只是配了子智能体或
    #   高风险工具的那些。
    outer.append(SuspensionMiddleware())
    if spec.limits.require_approval_for and approvals is not None:
        guarded = frozenset(approval_target(n) for n in spec.limits.require_approval_for)
        outer.append(ApprovalMiddleware(guarded, approvals))
    if compactor is not None:
        outer.append(compactor)

    inner: list[Any] = []
    # ★ sandbox 在 filesystem 之后、治理中间件之前：大结果外置挂在
    #   FilesystemMiddleware 的 wrap_tool_call 上，而它的主要服务对象正是
    #   execute（文件工具都在 TOOLS_EXCLUDED_FROM_EVICTION 里，它们自己有
    #   分页）。filesystem 必须在外层才接得住 sandbox 的大输出。
    if sandbox is not None:
        from atlas_engine.kernel.middleware.sandbox import SandboxMiddleware

        inner.append(SandboxMiddleware(sandbox=sandbox))
    # 延迟工具（上下文管理 §13）：MCP 工具多时只给名字，模型用 tool_search 按需加载定义
    if deferred_tools:
        from atlas_engine.kernel.middleware.tool_search import ToolSearchMiddleware

        inner.append(ToolSearchMiddleware(deferred_tools))
    inner.extend(middleware_for(set(spec.tool_names)))
    inner.append(StepLimitMiddleware(spec.limits.max_steps))
    inner.append(ToolGovernorMiddleware(concurrency=spec.limits.tool_concurrency))

    return create_deep_agent(
        model=model,
        system_prompt=system_prompt or None,
        tools=list(extra_tools) if extra_tools else None,
        # 空列表传 None：装一个内容为空的技能段只会白占提示词
        skills=list(skills) if skills else None,
        # 文件工具白名单：未勾 filesystem 传空表 —— 中间件保留（它是脚手架），
        # 模型看不到任何文件工具。execute 已不在此列，它归 SandboxMiddleware。
        # 没注入 filesystem 就传 None：没有存储 = 没有文件能力，白名单无从谈起。
        filesystem=(
            FilesystemConfig(
                filesystem=filesystem, tools=filesystem_tools_for(set(spec.tool_names))
            )
            if filesystem is not None
            else None
        ),
        middleware=[*outer, *inner],
        # 没配子智能体（或深度为 0）是 None：kernel 不装 task
        subagents=subagent_config,
        prompt_cache_ttl=prompt_cache_ttl,
    )
