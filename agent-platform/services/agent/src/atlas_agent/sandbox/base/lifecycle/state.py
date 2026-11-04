"""OpenSandbox 报的沙箱状态，分成六组。分组规则只在这里写一份。

★ 创建接口把 Allocated 改写成 Running 返回，GET 和列表却原样给 Allocated：两者都算可用。
★ 规范要求客户端容忍新增的状态值：不认识的状态既不用也不删（原先当坏的删，接入暂停后
  Paused 就会被误删）。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["SandboxState"]


class SandboxState(StrEnum):
    PENDING = "Pending"
    PAUSING = "Pausing"
    RESUMING = "Resuming"
    ALLOCATED = "Allocated"
    RUNNING = "Running"
    PAUSED = "Paused"
    STOPPING = "Stopping"
    TERMINATED = "Terminated"
    FAILED = "Failed"
    #: 不认识的新状态：不用、不删
    UNKNOWN = "Unknown"

    @classmethod
    def parse(cls, raw: object) -> SandboxState:
        try:
            return cls(str(raw))
        except ValueError:
            return cls.UNKNOWN

    @property
    def settling(self) -> bool:
        """过渡态：等它落定，不当坏的删（可能是另一个请求正在建）。"""
        return self in (SandboxState.PENDING, SandboxState.PAUSING, SandboxState.RESUMING)

    @property
    def usable(self) -> bool:
        return self in (SandboxState.ALLOCATED, SandboxState.RUNNING)

    @property
    def leaving(self) -> bool:
        """已经在删 / 删完了。"""
        return self in (SandboxState.STOPPING, SandboxState.TERMINATED)

    @property
    def broken(self) -> bool:
        return self is SandboxState.FAILED
