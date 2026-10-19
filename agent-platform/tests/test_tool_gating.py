"""Sandbox S0 · 文件工具与 execute 的门禁（docs/sandbox.md §2）。

两个实测发现的缺口：
  1. kernel 无条件装 FilesystemMiddleware —— 未勾 filesystem 的 agent
     也能 write_file，工具目录说谎
  2. execute 是「注册后运行时报错」而非「不注册」—— 模型看得见它，
     每次尝试烧一轮 token

修法是文件工具白名单（现为 FilesystemConfig.tools）贯通三处注入点。
全部用**行为断言**（让模型真调一次，看拿到什么）—— P6 的教训：
查图结构在这类问题上会写出永远通过的空断言。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from atlas_agent.domain.events import EventType
from tests.graphs import run_agent as run
from atlas_agent.domain.spec import AgentSpec, LimitSpec, ModelSpec

from atlas_agent.providers.filesystem.base import MemoryFilesystem
from langchain_core.messages import AIMessageChunk

from .fakes import TurnModel, tool_call_chunk

RUN_ID = UUID("66666666-6666-6666-6666-666666666666")

NOT_A_TOOL = "not a valid tool"


def _spec(tools: tuple[str, ...], **over: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "slug": "gated",
        "name": "门禁",
        "system_prompt": "p",
        "model": ModelSpec(model="claude-sonnet-5"),
        "tool_names": tools,
        "limits": LimitSpec(timeout_s=20),
    }
    base.update(over)
    return AgentSpec(**base)


async def _call_tool(spec: AgentSpec, tool: str, args: str, **hook_kw: Any) -> str:
    """让模型硬调一次工具，返回它拿到的结果文本。

    ★ 勾了 filesystem 就注入一个内存实现。没有 filesystem 配置 = 没有文件
      能力（不再回落 StateBackend），所以"勾了就得能用"的用例必须给它一个。
    """
    from atlas_agent.providers.filesystem.base import MemoryFilesystem

    if "filesystem" in spec.tool_names and "filesystem" not in hook_kw:
        hook_kw["filesystem"] = MemoryFilesystem()
    model = TurnModel(
        scripts=[
            [tool_call_chunk(tool, args, "c1")],
            [AIMessageChunk(content="完")],
        ]
    )
    events = [
        e
        async for e in run(
            spec,
            run_id=RUN_ID,
            model=model,
            input_content="去",
            **hook_kw,
        )
    ]
    for event in events:
        if event.type in (
            EventType.TOOL_COMPLETED,
            EventType.TOOL_FAILED,
            EventType.SUBAGENT_FINISHED,
        ):
            return str(event.data.get("result", ""))
    return ""


# ---------------------------------------------------------------- 缺口 1：文件工具


async def test_write_file_absent_without_filesystem() -> None:
    """★ 缺口 1：未勾 filesystem，write_file 必须**不存在**。

    修复前它会成功执行（kernel 无条件装 FilesystemMiddleware）。
    """
    result = await _call_tool(_spec(()), "write_file", '{"file_path":"/x.txt","content":"hi"}')
    assert NOT_A_TOOL in result, f"未勾 filesystem 却能写文件：{result[:80]}"


async def test_ls_absent_without_filesystem() -> None:
    result = await _call_tool(_spec(()), "ls", '{"path":"/"}')
    assert NOT_A_TOOL in result


async def test_write_file_works_when_picked() -> None:
    """勾了就得能用 —— 门禁不能把正常路径也关掉。"""
    result = await _call_tool(
        _spec(("filesystem",)), "write_file", '{"file_path":"/x.txt","content":"hi"}'
    )
    assert NOT_A_TOOL not in result
    assert "/x.txt" in result


async def test_file_written_event_still_flows() -> None:
    """勾了 filesystem 后 Inspector 文件页的数据源不受门禁改动影响。"""
    model = TurnModel(
        scripts=[
            [tool_call_chunk("write_file", '{"file_path":"/y.txt","content":"数据"}', "c1")],
            [AIMessageChunk(content="完")],
        ]
    )
    events = [
        e
        async for e in run(
            _spec(("filesystem",)),
            run_id=RUN_ID,
            model=model,
            input_content="去",
            filesystem=MemoryFilesystem(),
        )
    ]
    written = [e.data["path"] for e in events if e.type is EventType.FILE_WRITTEN]
    assert "/y.txt" in written


# ---------------------------------------------------------------- 缺口 2：execute


async def test_execute_absent_without_sandbox() -> None:
    """★ 缺口 2：没有 sandbox backend 时 execute 必须**不注册**。

    修复前是「注册 + 运行时报 Execution not available」——
    模型看得见工具，每次尝试烧一轮 token，且可能反复试。
    """
    result = await _call_tool(_spec(()), "execute", '{"command":"ls"}')
    assert NOT_A_TOOL in result, f"execute 泄漏：{result[:80]}"


async def test_execute_absent_even_with_filesystem() -> None:
    """勾了 filesystem 也不该有 execute —— 它只随 Sandbox backend 出现（S1）。"""
    result = await _call_tool(_spec(("filesystem",)), "execute", '{"command":"ls"}')
    assert NOT_A_TOOL in result, f"execute 混进了 filesystem 白名单：{result[:80]}"


# ---------------------------------------------------------------- 子智能体路径
#
# 图内子智能体的门禁测试（write_file 白名单 / execute 恒缺席）已随 engine 层
# 图内装配一起删除。那三条保证没有消失，而是换了住处：子智能体现在跑在
# **自己的子 run** 里，其 spec 由 spec_for_subagent 派生 —— tool_names 原样
# 携带（test_subagent_spec 钉着），随后走 build_agent 主路径，而主路径的
# 白名单正是本文件上面那批测试钉着的。同一套门禁，不再有第二份装配。

# ═══════════════════════════ 中间件拆分后的新边界 ═══════════════════════════


async def test_search_replaces_ls_glob_grep() -> None:
    """文件工具面收敛为 5 个：三个检索工具合成 search。

    ls/glob/grep 必须**不存在** —— 对象存储上 glob 要靠客户端过滤、
    grep 根本做不到，留着它们等于让模型调一个注定降级的工具。
    """
    spec = _spec(("filesystem",))
    got = await _call_tool(spec, "search", '{"prefix": ""}')
    assert "not found" not in got.lower()

    for gone in ("ls", "glob", "grep"):
        assert "Error:" in await _call_tool(spec, gone, "{}")


async def test_search_does_not_claim_content_search() -> None:
    """search 的描述必须把「不搜内容」写明并指向 execute。

    不写的话模型会反复用它找内容、拿到空结果、然后据此下「没找到相关
    文件」的结论 —— 错得很安静，而且从事件流上看不出异常。
    """
    from atlas_engine.kernel.middleware.filesystem import SEARCH_TOOL_DESCRIPTION

    assert "does not search file contents" in SEARCH_TOOL_DESCRIPTION
    assert "execute" in SEARCH_TOOL_DESCRIPTION


def test_bash_requires_filesystem() -> None:
    """K8s 沙箱接入后 bash 已实现；它的 /workspace 就是文件工具的工作区，所以仍要求勾 filesystem。

    部署是否开启沙箱不在这里判：没开时装配期明确报错（SandboxNotEnabled），见 test_sandbox_provider。
    """
    from atlas_agent.domain.tool_registry import unsupported_tools

    assert unsupported_tools(_spec(("bash",))) == ["bash"]
    assert unsupported_tools(_spec(("bash", "filesystem"))) == []


def test_filesystem_and_sandbox_middlewares_are_independent() -> None:
    """两个中间件各自独立装配，互不为前提。

    filesystem 是恒装的脚手架（大结果外置挂在它上面），
    恒装；sandbox 只在注入了 SandboxProtocol 时才装 —— 没有沙箱是合法状态。
    """
    from atlas_engine.kernel.middleware.filesystem import FilesystemMiddleware
    from atlas_engine.kernel.middleware.sandbox import SandboxMiddleware

    fs = FilesystemMiddleware(filesystem=MemoryFilesystem())
    assert [t.name for t in fs.tools] == [
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "search",
    ]

    class _Box:
        id = "probe"

        def execute(self, command, *, timeout=None, cwd=None):  # noqa: ANN001, ANN202
            raise NotImplementedError

        async def aexecute(self, command, **kw):  # noqa: ANN001, ANN202
            raise NotImplementedError

    assert [t.name for t in SandboxMiddleware(sandbox=_Box()).tools] == ["execute"]


async def test_the_model_sees_execute_when_a_sandbox_is_configured() -> None:
    """★ 配了沙箱，execute 必须出现在**绑定给模型的**工具里，而不只是注册在工具节点上。

    2026-10-19 排查死代码时发现：FilesystemMiddleware 还留着 execute 归它管时的能力过滤 ——
    文件系统对象不实现 SandboxProtocol 就把 execute 从模型请求里剔掉。生产上的
    OssFilesystem 从不实现它，于是配了 bash 的智能体模型侧根本看不到 execute。
    上面那批测试用假模型硬发 tool_call，绑没绑定都照样执行，所以一直没撞上 ——
    这条只看模型拿到了哪些工具。
    """
    from uuid import uuid4

    bound: list[list[str]] = []

    class _Recorder(TurnModel):
        def bind_tools(self, tools, **kwargs):  # type: ignore[no-untyped-def]
            bound.append(sorted(getattr(t, "name", None) or t["name"] for t in tools))
            return self

    class _Box:
        id = "probe"

        def execute(self, command, *, timeout=None):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def aexecute(self, command, *, timeout=None):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    model = _Recorder(scripts=[[AIMessageChunk(content="好")]])
    async for _ in run(
        _spec(("filesystem", "bash")),
        run_id=uuid4(),
        model=model,
        input_content="跑一下",
        filesystem=MemoryFilesystem(),
        sandbox=_Box(),
    ):
        pass
    assert bound and "execute" in bound[0], bound
