"""SandboxMiddleware —— 只提供 `execute` 工具。

从 `FilesystemMiddleware` 拆出来。拆分前 execute 和 8 个文件工具挤在同一个
中间件里，而两者在四个维度上性质不同：

  · **生命周期**：文件工具只要有对象存储就能跑，execute 必须有沙箱。
    混在一起只能靠运行时 `supports_execution(backend)` 判断，而那是个
    应该在装配期就分开的事。
  · **权限模型**：`FilesystemPermission` 按路径 glob 拦截文件工具，
    对 execute **完全不生效** —— shell 命令的路径无法静态分析。
    中间件叫 filesystem、execute 在里面、权限规则也在里面，很容易让人
    以为规则管住了 bash。拆开之后这个错觉自动消失。
  · **失败语义**：文件工具失败是工具错误（回给模型继续），
    沙箱起不来是 run.failed。
  · **审批粒度**：execute 在 schema 层被强制入审批列表，文件工具不审批。

★ 不进 `_REQUIRED_MIDDLEWARE`：没有沙箱是合法状态（顾问型 agent 可以
  只有文件工具）。这一点与 FilesystemMiddleware 不同 —— 后者是脚手架。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from atlas_engine.contracts import ExecuteArtifact, ExecuteResponse, SandboxProtocol

__all__ = ["EXECUTE_TOOL_DESCRIPTION", "SandboxMiddleware"]

#: 单命令超时的上限（秒）。模型可以按次传更小的值，传更大的会被拒。
DEFAULT_MAX_EXECUTE_TIMEOUT = 3600

EXECUTE_TOOL_DESCRIPTION = """Runs a shell command in an isolated sandbox and returns its output.

Usage:
- The working directory is the session workspace. Relative paths resolve there.
- **Content search lives here.** The `search` tool only matches paths and file
  names — to search inside files use `grep`/`rg` through this tool.
- Prefer `read_file` / `write_file` / `edit_file` over `cat` / `echo >` / `sed -i`:
  they page large files, report errors precisely, and go straight to the
  workspace without spawning a process.
- Pass `timeout` (seconds) for commands that may hang. Omit it for the default.
- The sandbox has no network access.
"""


class ExecuteSchema(BaseModel):
    """Input schema for the `execute` tool."""

    command: str = Field(description="Shell command to run.")
    timeout: int | None = Field(
        default=None,
        description="Per-command timeout in seconds. Omit for the backend default.",
    )


def _format_output(output: str, exit_code: int | None, *, truncated: bool) -> str:
    """把原始输出加上状态与截断说明。

    退出码必须显式告诉模型 —— 只给 stdout 的话，一条失败的命令看起来和
    一条没有输出的成功命令一模一样。
    """
    parts = [output]
    if exit_code is not None:
        parts.append(f"\n[Command {'succeeded' if exit_code == 0 else 'failed'} with exit code {exit_code}]")
    if truncated:
        parts.append("\n[Output was truncated due to size limits]")
    return "".join(parts)


def _artifact(response: ExecuteResponse) -> ExecuteArtifact:
    """退出码未知时**省略**该字段，而不是发一个 None。

    发 None 会让下游分不清「命令没报退出码」与「退出码就是 0」。
    """
    if response.exit_code is None:
        return {}
    return {"exit_code": response.exit_code}


def _err(message: str, tool_call_id: str | None) -> ToolMessage:
    return ToolMessage(content=message, name="execute", tool_call_id=tool_call_id, status="error")


class SandboxMiddleware(AgentMiddleware):
    """向模型提供 `execute`。不提供任何文件操作。"""

    def __init__(
        self,
        *,
        sandbox: SandboxProtocol,
        max_execute_timeout: int = DEFAULT_MAX_EXECUTE_TIMEOUT,
        tool_description: str | None = None,
        system_prompt: str | None = None,
    ) -> None:
        if max_execute_timeout <= 0:
            msg = f"max_execute_timeout must be positive, got {max_execute_timeout}"
            raise ValueError(msg)
        super().__init__()
        self._sandbox = sandbox
        self._max_execute_timeout = max_execute_timeout
        self._description = tool_description or EXECUTE_TOOL_DESCRIPTION
        self.system_prompt = system_prompt
        self.tools: list[BaseTool] = [self._create_execute_tool()]

    def _check_timeout(self, timeout: int | None, tool_call_id: str | None) -> ToolMessage | None:
        if timeout is None:
            return None
        if timeout < 0:
            return _err(f"Error: timeout must be non-negative, got {timeout}.", tool_call_id)
        if timeout > self._max_execute_timeout:
            return _err(
                f"Error: timeout {timeout}s exceeds maximum allowed "
                f"({self._max_execute_timeout}s).",
                tool_call_id,
            )
        return None

    def _create_execute_tool(self) -> BaseTool:
        sandbox = self._sandbox

        def sync_execute(
            command: str,
            runtime: ToolRuntime[None, Any],
            timeout: int | None = None,
        ) -> ToolMessage:
            if (bad := self._check_timeout(timeout, runtime.tool_call_id)) is not None:
                return bad
            try:
                response = sandbox.execute(command, timeout=timeout)
            except NotImplementedError as exc:
                return _err(f"Error: Execution not available. {exc}", runtime.tool_call_id)
            except ValueError as exc:
                return _err(f"Error: Invalid parameter. {exc}", runtime.tool_call_id)
            return ToolMessage(
                content=_format_output(
                    response.output, response.exit_code, truncated=response.truncated
                ),
                name="execute",
                tool_call_id=runtime.tool_call_id,
                artifact=_artifact(response),
                status="success",
            )

        async def async_execute(
            command: str,
            runtime: ToolRuntime[None, Any],
            timeout: int | None = None,  # noqa: ASYNC109  # 转交后端，不是 asyncio 契约
        ) -> ToolMessage:
            if (bad := self._check_timeout(timeout, runtime.tool_call_id)) is not None:
                return bad
            try:
                response = await sandbox.aexecute(command, timeout=timeout)
            except NotImplementedError as exc:
                return _err(f"Error: Execution not available. {exc}", runtime.tool_call_id)
            except ValueError as exc:
                return _err(f"Error: Invalid parameter. {exc}", runtime.tool_call_id)
            return ToolMessage(
                content=_format_output(
                    response.output, response.exit_code, truncated=response.truncated
                ),
                name="execute",
                tool_call_id=runtime.tool_call_id,
                artifact=_artifact(response),
                status="success",
            )

        return StructuredTool.from_function(
            name="execute",
            description=self._description,
            func=sync_execute,
            coroutine=async_execute,
            infer_schema=False,
            args_schema=ExecuteSchema,
        )
