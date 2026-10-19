"""图这一侧（kernel）：契约、execute 工具、文件工具、组装智能体。

图不认识 Pod、cluster、HTTP，只认 SandboxProtocol 一个契约
（对应 atlas_engine/contracts/sandbox.py）：

    execute      SandboxMiddleware 提供。把 tool_call_id 作为 exec_id 交给沙箱 ——
                 同一个调用被重放时，沙箱据此接上原命令而不是再跑一遍。
                 SandboxUnavailable 转成工具错误，不让整轮失败
    write_file   FilesystemMiddleware 提供。直连对象存储、不经过沙箱：没有沙箱的会话照样能用，
    read_file    读写文件也不必为此起一个 Pod（契约拆分：沙箱只管执行）

★ 文件和命令走两条路，看到的是同一份数据：沙箱的 /workspace 挂的就是这个对象存储前缀。
  两条路之间的缓存窗口由 sandboxd 的屏障收口（storage.py），图这边一无所知。
★ 这个文件不能写 from __future__ import annotations：LangChain 靠运行时的注解类型认出要注入的
  runtime: ToolRuntime 参数，注解变成字符串就认不出来，工具一调用就缺参数（真实代码踩过）。
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
from storage import ObjectStorage
from tracing import say

# ─────────────────────────────────────────────────────────────── 契约


class SandboxUnavailable(RuntimeError):
    """沙箱此刻不可用（Pod 起不来、配额满、连不上、中途被删）。转成工具错误，不让整轮失败。"""


@dataclass
class ExecuteResponse:
    output: str  # stdout + stderr
    exit_code: int | None = None  # None = 不知道
    truncated: bool = False


class SandboxProtocol(Protocol):
    """在隔离环境里执行命令。不提供任何文件操作。"""

    @property
    def id(self) -> str: ...

    async def aexecute(
        self, command: str, *, timeout: int | None = None, exec_id: str | None = None
    ) -> ExecuteResponse: ...


# ─────────────────────────────────────────────────────────────── execute

MAX_EXECUTE_TIMEOUT = 3600  # 模型可以传更小的 timeout，传更大的被拒。不传 = 不限


class ExecuteSchema(BaseModel):
    command: str = Field(description="Shell command to run.")
    timeout: int | None = Field(default=None, description="Seconds. Omit for no limit.")


def _format_output(response: ExecuteResponse) -> str:
    """退出码必须显式告诉模型：只给输出的话，失败的命令和没有输出的成功命令看起来一样。"""
    parts = [response.output]
    if response.exit_code is not None:
        ok = "succeeded" if response.exit_code == 0 else "failed"
        parts.append(f"\n[Command {ok} with exit code {response.exit_code}]")
    if response.truncated:
        parts.append("\n[Output was truncated due to size limits]")
    return "".join(parts)


class SandboxMiddleware(AgentMiddleware):
    """向模型提供 execute。★ 不进必需中间件：没有沙箱是合法状态（只有文件工具的智能体）。"""

    def __init__(self, sandbox: SandboxProtocol) -> None:
        super().__init__()
        self.tools = [self._execute_tool(sandbox)]

    @staticmethod
    def _execute_tool(sandbox: SandboxProtocol) -> StructuredTool:
        async def execute(
            command: str, runtime: ToolRuntime[None, Any], timeout: int | None = None
        ) -> ToolMessage:
            call_id = runtime.tool_call_id
            if timeout is not None and not 0 < timeout <= MAX_EXECUTE_TIMEOUT:
                return ToolMessage(
                    f"Error: timeout must be in (0, {MAX_EXECUTE_TIMEOUT}]",
                    tool_call_id=call_id,
                    name="execute",
                    status="error",
                )
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
            return ToolMessage(
                _format_output(response),
                tool_call_id=call_id,
                name="execute",
                artifact={} if response.exit_code is None else {"exit_code": response.exit_code},
            )

        def execute_sync(command: str, runtime: ToolRuntime[None, Any], timeout: int | None = None):
            raise NotImplementedError("沙箱只支持异步调用")

        return StructuredTool.from_function(
            name="execute",
            description="Runs a shell command in an isolated sandbox. cwd is the session "
            "workspace. There is no default time limit; pass `timeout` only for commands "
            "that may hang.",
            func=execute_sync,
            coroutine=execute,
            infer_schema=False,
            args_schema=ExecuteSchema,
        )


# ─────────────────────────────────────────────────────────────── 文件工具


class FilesystemMiddleware(AgentMiddleware):
    """文件工具直连对象存储：会话工作区就是一个前缀。"""

    def __init__(self, storage: ObjectStorage, prefix: str) -> None:
        super().__init__()

        @tool
        async def write_file(path: str, content: str) -> str:
            """写一个工作区文件。"""
            storage.put(prefix + path, content.encode())
            say("文件工具", f"write_file {path} → 对象存储 {prefix}{path}")
            return f"已写入 {path}"

        @tool
        async def read_file(path: str) -> str:
            """读一个工作区文件。"""
            data = storage.get(prefix + path)
            found = "读到了" if data is not None else "没有这个对象"
            say("文件工具", f"read_file {path} ← 对象存储：{found}")
            return data.decode() if data is not None else f"Error: 文件不存在：{path}"

        self.tools = [write_file, read_file]


def build_agent(sandbox: SandboxProtocol, storage: ObjectStorage, prefix: str) -> Any:
    return create_agent(
        model=ScriptedModel(),
        middleware=[FilesystemMiddleware(storage, prefix), SandboxMiddleware(sandbox)],
    )
