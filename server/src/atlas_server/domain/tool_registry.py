"""内置工具注册表 —— 工具知识的唯一来源。

此前同一个工具的信息散落三处：agent.py 的 SUPPORTED_TOOLS frozenset、
_middleware_for 的 if 链、server/services/meta.py 手写的目录条目。
三处靠人肉同步，编译器不会提醒漏了哪处 —— 工具目录「说谎」
（web_search 标可用但 build_agent 不认）就是这么产生的。

现在加一个工具 = 在 BUILTIN_TOOLS 加一个 ToolDef。目录、中间件装配、
可用性判断全部从这张表派生。

★ 分层注意：display_name / description 是给 /v1/tools 目录用的展示文案。
  它们放在 engine 是刻意的取舍 —— 拆到 server 会让「加一个工具」重新
  变成改两处。engine 的错误消息本就面向用户（中文），这里保持一致。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .spec import AgentSpec

#: MCP 工具的命名前缀：mcp:<server>:<tool>。
#: 它们由 server 解析后以 extra_tools 注入，不在本表内。
MCP_PREFIX = "mcp:"

from atlas_engine.kernel.middleware.subagents import SUBAGENT_TOOL  # noqa: F401 —— 单一来源在 kernel

TODO_TOOL = "write_todos"
FILESYSTEM_TOOL = "filesystem"
BASH_TOOL = "bash"
WEB_SEARCH_TOOL = "web_search"
MEMORY_SEARCH_TOOL = "search_memory"


def _todo_middleware() -> Any:
    # 待办清单在**上游 langchain**，不在 kernel —— 这个分工不明显，踩过一次
    from langchain.agents.middleware import TodoListMiddleware

    return TodoListMiddleware()


#: filesystem 的文件工具。★ 刻意不含 execute —— 它归 SandboxMiddleware，
#: 只随 sandbox 出现，在此之前模型根本不该看到它（Sandbox 设计 §2 缺口 2）。
#:
#: 没有 ls / glob / grep：三者合成 search（前缀 + 文件名 glob）。
#: 内容检索对象存储做不到，归 execute —— 见 SEARCH_TOOL_DESCRIPTION。
FS_TOOL_NAMES: tuple[str, ...] = (
    "read_file",
    "write_file",
    "edit_file",
    "delete",
    "search",
)


def filesystem_tools_for(tool_names: frozenset[str] | set[str]) -> list[str]:
    """交给 create_deep_agent(filesystem_tools=) 的白名单。

    FilesystemMiddleware 是 kernel 的 _REQUIRED_MIDDLEWARE 脚手架
    （大结果外置、权限机制挂在上面），**不能整个移除** —— 未勾 filesystem 时
    传空表：中间件保留、模型看不到任何文件工具。

    ★ 取代了原来的 filesystem_allowlist(tool_names, sandbox=)。那个函数既管
      白名单又管 execute，还藏着一条隐式联动：**勾了 bash 就自动获得全部
      文件工具**（理由是「bash 写的文件 read_file 要能读到」）。拆分后
      execute 归 SandboxMiddleware，那条联动改成 bash 的 requires 显式声明 ——
      不满足时明确报出来，而不是偷偷替用户勾上。
    """
    return list(FS_TOOL_NAMES) if FILESYSTEM_TOOL in tool_names else []


@dataclass(frozen=True)
class ToolDef:
    """一个内置工具的全部知识。

    implemented: 本期是否接入 —— 目录里的 available 列。
    middleware:  装配进 agent 图的中间件工厂；None 表示该工具不经中间件
                 提供（task 走 create_deep_agent(subagents=)，见 agent.py）。
    requires:    spec 层面的额外前提。不满足时该工具视同未接入，
                 会出现在 unsupported_tools() 里 —— §13.2 不允许
                 「勾了却静默不生效」。
    model_tool_names:
                 该注册项在**模型侧**实际出现的工具名。多数情况与 name 相同，
                 两处例外：bash 的模型侧名是 execute（D4），filesystem 展开成
                 7 个文件工具。★ require_approval_for 匹配的是这些名字，
                 不是注册项的 name —— 编辑器要按它渲染审批勾选项。
    """

    name: str
    display_name: str
    description: str
    implemented: bool
    note: str | None = None
    middleware: Callable[[], Any] | None = None
    requires: Callable[[AgentSpec], bool] | None = None
    model_tool_names: tuple[str, ...] = ()

    def approval_targets(self) -> tuple[str, ...]:
        """可作为审批目标的模型侧工具名；未显式声明时就是 name 本身。"""
        return self.model_tool_names or (self.name,)


#: ★ 加新工具改这里，其余全部派生。顺序即 /v1/tools 的展示顺序。
BUILTIN_TOOLS: tuple[ToolDef, ...] = (
    ToolDef(
        name=TODO_TOOL,
        display_name="待办清单",
        description="内核计划能力，TodoCard 的数据源",
        implemented=True,
        middleware=_todo_middleware,
    ),
    ToolDef(
        name=FILESYSTEM_TOOL,
        model_tool_names=FS_TOOL_NAMES,
        display_name="虚拟文件系统",
        description="read_file / write_file / ls，中间产物落盘",
        implemented=True,
        # middleware=None：文件工具由 kernel 的 FilesystemMiddleware 提供
        # （它是必备脚手架，backend/外置/权限都接在上面），本表只决定
        # 白名单 —— 见 filesystem_allowlist 与 agent.build_agent。
    ),
    ToolDef(
        name=SUBAGENT_TOOL,
        display_name="子智能体委派",
        description="把子任务交给专门的子智能体，在独立上下文里执行",
        implemented=True,
        note="需要在下方配置至少一个子智能体，否则勾选无效",
        # 勾了 task 却没配子智能体 = 等于没开，必须报出来
        requires=lambda spec: bool(spec.subagents),
    ),
    ToolDef(
        name=WEB_SEARCH_TOOL,
        display_name="网页搜索",
        description="按关键词搜索网页，返回标题/链接/摘要 —— 模型自行决定搜几次、搜什么",
        implemented=True,
        note="SerpAPI，需要服务端配置 SERPAPI_KEY",
        # middleware=None：裸 API 工具经 hooks.extra_tools 注入（server 组装，凭据在 server）
    ),
    ToolDef(
        name=MEMORY_SEARCH_TOOL,
        display_name="记忆检索",
        description="回忆该用户在之前会话里提到过的偏好、习惯与项目约定",
        implemented=True,
        # ★ 只读。不提供 add / update / delete：写记忆会随 regenerate 重复
        #   执行（记忆设计 §05），管理操作是用户的权利而不是 Agent 的能力
        #   （§10）。纯读取则重跑多少次都不改变状态，可以安全挂上。
        note="需要服务端开启记忆（MEMORY_ENABLED）",
        # middleware=None：与 web_search 同款，经 hooks.extra_tools 注入
        #   —— user_id 要从会话上下文闭包捕获，那是 server 才有的东西。
    ),
    ToolDef(
        name=BASH_TOOL,
        # 模型看到的是 execute，审批中间件也按它匹配（D4）
        model_tool_names=("execute",),
        display_name="Shell",
        description="在会话专属的沙箱内执行命令（模型侧工具名为 execute）",
        # ★ Docker 实现已删除，K8s Pod 实现（acp 详设 providers/pods/）未接入 ——
        #   本期没有任何 SandboxProtocol 的实现方。标 False 而不是删掉这一项：
        #   协议与 SandboxMiddleware 都还在，接上 Pod 就是把 implemented 改回 True；
        #   期间勾了 bash 的 agent 会进 unsupported_tools 明确报出来（§13.2），
        #   而不是静默跑一个没有 execute 的 run。
        implemented=False,
        note="Docker 实现已移除，等 K8s Pod 执行环境接入",
        requires=lambda spec: FILESYSTEM_TOOL in spec.tool_names,
    ),
)

_BY_NAME: dict[str, ToolDef] = {t.name: t for t in BUILTIN_TOOLS}


def middleware_for(tool_names: frozenset[str] | set[str]) -> list[Any]:
    """按 spec.tool_names 装配中间件。未知名字在这里忽略 ——
    报告职责在 unsupported_tools()，装配职责在这里，不重复。"""
    out: list[Any] = []
    for tool in BUILTIN_TOOLS:
        if tool.name in tool_names and tool.middleware is not None:
            out.append(tool.middleware())
    return out


def unsupported_tools(spec: AgentSpec) -> list[str]:
    """spec 里请求了但实际不生效的工具。

    调用方应当把它报出来而不是静默忽略（§13.2）：
      · 未知名字 / 本期未接入（implemented=False）
      · requires 前提不满足（如 task 没配子智能体）
      · mcp:* 放行 —— 可用性由 server 侧判定，解析不到时自会报错
    """
    out: list[str] = []
    for name in spec.tool_names:
        if name.startswith(MCP_PREFIX):
            continue
        tool = _BY_NAME.get(name)
        unavailable = tool is None or not tool.implemented
        unmet = tool is not None and tool.requires is not None and not tool.requires(spec)
        if unavailable or unmet:
            out.append(name)
    return sorted(out)
