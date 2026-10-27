"""图这一侧（kernel）：两个契约、execute 工具、文件工具、组装智能体。

图不认识 OpenSandbox、Pod、路由，只认两个契约（对应 atlas_engine/contracts/）：

    SandboxProtocol     execute 用。把 tool_call_id 作为 exec_id 交给沙箱 —— 同一个调用被重放时，
                        沙箱据此接上原命令而不是再跑一遍；
                        SandboxUnavailable 转成工具错误，不让整轮失败
    FilesystemProtocol  文件工具用。它背后是直连对象存储还是经沙箱，图一无所知
                        （workspace.py 的路由）

★ 迁移到 OpenSandbox 之后，这个文件和迁移前一模一样 —— 这正是设计里「契约不变」的意思。
★ 这个文件不能写 from __future__ import annotations：LangChain 靠运行时的注解类型认出要注入的
  runtime: ToolRuntime 参数，注解变成字符串就认不出来（真实代码踩过）。
"""

from dataclasses import dataclass
from typing import Any, Protocol

from langchain.agents import create_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool, tool
from pydantic import BaseModel, Field
from scripted import ScriptedModel
from tracing import say

# ─────────────────────────────────────────────────────────────── 契约


class SandboxUnavailable(RuntimeError):
    """沙箱此刻不可用（起不来、配额满、连不上、中途被换掉）。转成工具错误，不让整轮失败。"""


@dataclass
class ExecuteResponse:
    output: str
    exit_code: int | None = None
    truncated: bool = False


class SandboxProtocol(Protocol):
    """在隔离环境里执行命令。不提供任何文件操作。"""

    @property
    def id(self) -> str: ...

    async def aexecute(
        self, command: str, *, timeout: int | None = None, exec_id: str | None = None
    ) -> ExecuteResponse: ...


class FilesystemProtocol(Protocol):
    """会话工作区的文件访问（真实的还有 search / edit / delete，demo 只留两个）。"""

    async def aread(self, path: str) -> str: ...

    async def awrite(self, path: str, content: str) -> str: ...


# ─────────────────────────────────────────────────────────────── execute


class ExecuteSchema(BaseModel):
    command: str = Field(description="Shell command to run.")
    timeout: int | None = Field(default=None, description="Seconds. Omit for no limit.")


def _format(response: ExecuteResponse) -> str:
    """退出码必须显式告诉模型：只给输出的话，失败的命令和没有输出的成功命令看起来一样。"""
    parts = [response.output]
    if response.exit_code is not None:
        ok = "succeeded" if response.exit_code == 0 else "failed"
        parts.append(f"\n[Command {ok} with exit code {response.exit_code}]")
    if response.truncated:
        parts.append("\n[Output was truncated due to size limits]")
    return "".join(parts)


class SandboxMiddleware(AgentMiddleware):
    def __init__(self, sandbox: SandboxProtocol) -> None:
        super().__init__()

        async def execute(
            command: str, runtime: ToolRuntime[None, Any], timeout: int | None = None
        ) -> ToolMessage:
            call_id = runtime.tool_call_id
            try:
                response = await sandbox.aexecute(command, timeout=timeout, exec_id=call_id)
            except SandboxUnavailable as exc:
                say("execute", f"{call_id} SandboxUnavailable → 转成工具错误回给模型（这一轮继续）")
                return ToolMessage(
                    f"Error: Sandbox unavailable. {exc}",
                    tool_call_id=call_id,
                    name="execute",
                    status="error",
                )
            return ToolMessage(_format(response), tool_call_id=call_id, name="execute")

        def execute_sync(
            command: str, runtime: ToolRuntime[None, Any], timeout: int | None = None
        ) -> None:
            raise NotImplementedError("沙箱只支持异步调用")

        self.tools = [
            StructuredTool.from_function(
                name="execute",
                description="Runs a shell command in an isolated sandbox. "
                "cwd is the session workspace. "
                "There is no default time limit; pass `timeout` only for commands that may hang.",
                func=execute_sync,
                coroutine=execute,
                infer_schema=False,
                args_schema=ExecuteSchema,
            )
        ]


# ─────────────────────────────────────────────────────────────── 文件工具


class FilesystemMiddleware(AgentMiddleware):
    def __init__(self, fs: FilesystemProtocol) -> None:
        super().__init__()

        @tool
        async def write_file(path: str, content: str) -> str:
            """写一个工作区文件。"""
            return await fs.awrite(path, content)

        @tool
        async def read_file(path: str) -> str:
            """读一个工作区文件。"""
            return await fs.aread(path)

        self.tools = [write_file, read_file]


def build_agent(sandbox: SandboxProtocol, fs: FilesystemProtocol) -> Any:
    return create_agent(
        model=ScriptedModel(), middleware=[FilesystemMiddleware(fs), SandboxMiddleware(sandbox)]
    )
