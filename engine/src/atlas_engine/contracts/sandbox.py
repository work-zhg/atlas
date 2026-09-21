"""SandboxProtocol —— 隔离环境里跑一条命令的契约。

★ 与 FilesystemProtocol 同住 contracts：见 filesystem.py 头部的归属说明。

与 `FilesystemProtocol` **并列**。拆分前是
`SandboxBackendProtocol(BackendProtocol)`，继承关系强迫「沙箱」同时是
「文件系统」，于是 `BaseSandbox` 用 `execute()` 现跑脚本实现了 13 个文件
操作 —— `read` 是往沙箱塞一段 Python、`ls` 是 `os.scandir` 脚本。每次
`read_file` 就是一次 `docker exec`（换 K8s 后是一次带协议升级的 API 调用）。

拆开之后沙箱只负责执行，文件走 `FilesystemProtocol` 直连对象存储，那批
委托代码整体消失。

★ 工作区的可见性由**挂载**保证，不由协议保证：沙箱的 `/workspace` 是会话
  OSS 前缀的 FUSE 挂载点，所以 `execute` 写的文件 `read_file` 能读到，
  反之亦然。协议本身对此一无所知 —— 这正是拆开的意义。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from typing_extensions import NotRequired, TypedDict

__all__ = [
    "DEFAULT_WORKDIR",
    "ExecuteArtifact",
    "ExecuteResponse",
    "SandboxProtocol",
]


@dataclass
class ExecuteResponse:
    """Result of code execution.

    Simplified schema optimized for LLM consumption.
    """

    output: str
    """Combined stdout and stderr output of the executed command."""

    exit_code: int | None = None
    """The process exit code.

    0 indicates success, non-zero indicates failure. `None` means the exit code
    could not be determined.
    """

    truncated: bool = False
    """Whether the output was truncated due to backend limitations."""

class ExecuteArtifact(TypedDict):
    """Machine-readable metadata attached to an `execute` tool result.

    Carried on `ToolMessage.artifact` alongside the model-facing `content`, so
    callers can react to shell failures. `artifact` is `None` instead when no
    command ran -- a validation or unsupported-backend error, where
    `ToolMessage.status` is `"error"`.

    Note that `status` is `"success"` for any command that ran, including one
    that exited non-zero: the model is expected to read the output and decide
    what to do. Use `exit_code`, not `status`, to detect command failure.
    """

    exit_code: NotRequired[int]
    """The command's exit status. 0 indicates success, non-zero indicates failure.

    Omitted when the exit code could not be determined.
    """

#: 沙箱内的工作目录，也是会话工作区的挂载点。
#: 与 `FilesystemProtocol` 的路径根对应同一份数据。
DEFAULT_WORKDIR = "/workspace"


@runtime_checkable
class SandboxProtocol(Protocol):
    """在隔离环境里执行命令。不提供任何文件操作。"""

    @property
    def id(self) -> str:
        """沙箱标识（容器名 / Pod 名）。用于日志与孤儿回收。"""
        ...

    def execute(
        self, command: str, *, timeout: int | None = None, cwd: str | None = None
    ) -> ExecuteResponse:
        """跑一条 shell 命令。

        Args:
            command: 命令行。
            timeout: 单命令超时（秒）。实现必须在**沙箱内部**也兜一层
                （如 `timeout N sh -c ...`）—— 只在客户端 kill 不会杀掉
                容器内的进程，命令会在里面跑到天荒地老。
            cwd: 工作目录，默认 `DEFAULT_WORKDIR`。
        """
        ...

    async def aexecute(
        self, command: str, *, timeout: int | None = None, cwd: str | None = None
    ) -> ExecuteResponse: ...
