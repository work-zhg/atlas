"""沙箱内执行的值对象和错误。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from ..lifecycle.runtime import SandboxGone

__all__ = [
    "CommandGone",
    "CommandSpec",
    "CommandStatus",
    "ExecdRejected",
    "ExecdUnreachable",
    "FileEntry",
    "LogChunk",
    "ResourceUsage",
    "SandboxActivity",
    "SandboxGone",
    "SubmitUnknown",
    "UsageScope",
]


class CommandGone(LookupError):
    """沙箱还在，但 execd 不认识这个命令 id：Pod 重建或容器重启过，命令记录只在 execd 的内存里。

    execd 的状态接口回 404、日志接口回 400，两种回法翻成这一个。
    """


class SubmitUnknown(RuntimeError):
    """提交时在拿到命令 id（init 事件）之前断开：命令可能已经开始，结果未知。不重试，免得跑两遍。"""


class ExecdUnreachable(ConnectionError):
    """这一次连不上 execd。地址和令牌的缓存已作废：下一次按同一个 sandbox_uuid 重取，

    沙箱还在就接着用原来的命令 id 和游标；已经不在会抛 SandboxGone。重试几次由调用方定。
    """


class ExecdRejected(RuntimeError):
    """execd 拒绝了请求（不是「连不上」，也不是「不认识这个命令」）。"""


@dataclass(frozen=True)
class CommandSpec:
    """要执行的命令。timeout_s 为 None 表示不限时（不设运行上限）。

    env 叠在沙箱的环境变量之上，只给这一条命令（如每个会话的 bridge 各自的配置）。
    """

    command: str
    cwd: str
    timeout_s: int | None = None
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LogChunk:
    """一段增量输出和下次接着读的游标。游标原样来自 execd 的响应头，调用方只保存、回传。"""

    data: bytes
    cursor: int


@dataclass(frozen=True)
class CommandStatus:
    running: bool
    exit_code: int | None
    error: str | None

    def failed_to_start(self) -> bool:
        """有错误但没有退出码：命令根本没启动起来。"""
        return not self.running and self.exit_code is None and bool(self.error)


UsageScope = Literal["sandbox", "node"]


@dataclass(frozen=True)
class ResourceUsage:
    """CPU 使用率，带统计口径。

    ★ OpenSandbox 1.1.1 的 /metrics 读宿主的 /proc，是整个节点的数字；1.2.0 在设了资源上限时才改用
      沙箱自己的 cgroup。整节点口径时卡死判定不下结论，免得被别的 Pod 的负载带偏。
    """

    cpu_pct: float
    scope: UsageScope

    def per_sandbox(self) -> bool:
        return self.scope == "sandbox"


@dataclass(frozen=True)
class SandboxActivity:
    """一个沙箱此刻的活动：确认过仍在跑的命令，和最近一次活动的时间。"""

    running: frozenset[str]
    last_active_at: datetime

    def busy(self) -> bool:
        return bool(self.running)

    def idle_for(self, now: datetime) -> float:
        return max(0.0, (now - self.last_active_at).total_seconds())


@dataclass(frozen=True)
class FileEntry:
    """沙箱里的一个文件（绝对路径）。"""

    path: str
    size: int
