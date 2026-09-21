"""spec + 能力对象 → agent 图。装配策略的唯一住所。

前身是 engine 的 `agent.py`（防腐层）。kernel 永久内化之后那层防腐失去了
对象，装配策略随之收进 server —— **知道配置、知道能力从哪来的层才有资格
决定装什么、什么顺序**。engine 只剩词汇（spec / events）与执行循环（runner）。

★ 本模块刻意保持纯净：只 import engine / kernel，不碰 DB、Redis、Settings。
  测试用假模型 + 假能力对象即可脱离一切基础设施装配出真实的图 ——
  IO 的收集在 assembly.py，那里才认识 boto3 与 Redis。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from atlas_engine.contracts import InvalidSpec
from atlas_engine.kernel.middleware.approval import ApprovalMiddleware
from atlas_engine.kernel.middleware.limits import StepLimitMiddleware, ToolGovernorMiddleware

from atlas_server.domain.tool_registry import FILESYSTEM_TOOL, filesystem_tools_for, middleware_for

if TYPE_CHECKING:
    from atlas_engine.contracts import (
        ApprovalGate,
        DelegationProtocol,
        FilesystemProtocol,
        SandboxProtocol,
        SkillRef,
    )
    from langchain_core.language_models import BaseChatModel

    from atlas_server.domain.spec import AgentSpec

__all__ = ["build_graph"]

#: 文件工具那一段。它与沙箱无关 —— 没配 bash 的 agent 也有工作区。
_FILESYSTEM_HINT = (
    "文件工具作用于会话工作区，内容跨轮保留。"
    "search 只匹配路径与文件名，**不搜索文件内容** —— 要按内容查找，用 execute 跑 grep。"
)

#: 沙箱那一段。
#:
#: ★ 过渡说明（方案 A）：FUSE 挂载要等 K8s 就绪，在那之前沙箱的 /workspace
#:   是**容器本地盘**，与文件工具的工作区不是同一份数据。这件事必须明写 ——
#:   让模型以为两者相通，它会 write_file 之后去 cat，然后开始"修"一个
#:   不存在的问题。FUSE 落地后删掉中间那段，其余不动。
_SANDBOX_HINT = (
    "execute 在隔离沙箱内执行，无网络，容器根只读，可写区是 /workspace 与 /tmp。"
    "\n**注意：沙箱的 /workspace 是容器本地盘，与上述文件工具的工作区当前不是同一份数据。**"
    "需要长期保留的产物请用 write_file 写入工作区；容器内的文件随容器回收而消失。"
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
    subagents: DelegationProtocol | None = None,
) -> Any:
    """按 spec 组装 agent 图。返回 CompiledStateGraph，交给 runner.run 驱动。

    能力对象一律可缺省 —— None 的含义是**该能力结构性不存在**（对应工具
    不注册），不是"注册后调用才报错"：后者模型看得见、每次尝试烧一轮 token。
    """
    from atlas_engine.kernel.graph import create_deep_agent

    system_prompt = spec.system_prompt or ""
    if FILESYSTEM_TOOL in spec.tool_names:
        system_prompt = f"{system_prompt}\n\n{_FILESYSTEM_HINT}".strip()
    if sandbox is not None:
        system_prompt = f"{system_prompt}\n\n{_SANDBOX_HINT}".strip()

    # max_subagent_depth=0 表示不允许委派 —— 直接不装子智能体，
    # 而不是装上再在调用时拒绝（后者会让模型反复尝试一个注定失败的工具）
    subagent_specs: list[Any] = []
    if spec.subagents and spec.limits.max_subagent_depth >= 1:
        if subagents is None:
            # §13.2 不允许静默降级：配了子智能体却没注入受理方，砍掉 task
            # 会让模型按「没有帮手」的方式做事，而配置界面显示它有 —— 报错。
            raise InvalidSpec(
                "配置了子智能体但未注入委派受理方（DelegationProtocol）；"
                "assembly 注入 SubagentService，单测注入实现该协议的假对象",
                subagents=[sub.name for sub in spec.subagents],
            )
        # 只交 name / description 给 task 的工具描述 —— 子智能体的模型、
        # 中间件、权限都在**子 run** 里按 spec_for_subagent 派生的 AgentSpec
        # 重新装配（detail/subagent.html §09）。
        subagent_specs = [
            {"name": sub.name, "description": sub.description, "system_prompt": sub.system_prompt}
            for sub in spec.subagents
        ]

    # ★ 中间件顺序即语义，从外到内分两层显式列出（列表顺序 = 包裹顺序）：
    #
    #   外层（顺序敏感）：
    #     1. approval  必须最外 —— 排后面的话工具已被内层执行掉，再问也来不及
    #     2. compactor 次之 —— abefore_model 要在其它中间件动手前拿到完整消息列表
    #   内层（彼此无序）：
    #     工具中间件 + 步数上限 + 工具并发/熔断（§4.4 / §13.2）
    #
    # 新增有序中间件时把它插进 outer 的正确位置并写明理由，不要用 insert(0)。
    outer: list[Any] = []
    if spec.limits.require_approval_for and approvals is not None:
        outer.append(ApprovalMiddleware(frozenset(spec.limits.require_approval_for), approvals))
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
        filesystem_tools=filesystem_tools_for(set(spec.tool_names)),
        backend=filesystem,
        middleware=[*outer, *inner],
        # 空列表要传 None：kernel 对空 subagents 不装 task
        subagents=subagent_specs or None,
        subagent_delegate=subagents,
    )
