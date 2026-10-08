"""ResumePlanner：按 agent 能力位选择打开会话的 ACP 方法（Bridge 设计 §5.6）。

纯函数，§5.6 的表逐行对应一个测试用例。「新建还是恢复、恢复哪个」由 server 决定；
「用 load 还是 resume」由这里按能力位决定。原则：尽力满足，但从不假装 ——
实际做了什么，如实写进 ``Plan.replayed``。

只覆盖 ACP v1（bridge 目前只协商 v1）；v2 的 ``session/resume + replayFrom`` 以后在此加一行。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from atlas_acp.v1 import AgentCaps

__all__ = ["OpenMethod", "Plan", "ReplayWant", "plan_open"]

ReplayWant = Literal["none", "full"]


class OpenMethod(StrEnum):
    NEW = "new"
    LOAD = "load"
    RESUME = "resume"


@dataclass(frozen=True, slots=True)
class Plan:
    method: OpenMethod
    #: 这个方法实际会不会重放历史（server 要不要是另一回事）
    replayed: ReplayWant


def plan_open(caps: AgentCaps, want: ReplayWant | None) -> Plan:
    """``want`` 为 None 表示新建会话；否则是 server 对历史的需求。"""
    if want is None:
        return Plan(OpenMethod.NEW, "none")
    if want == "none":
        if caps.resume:
            return Plan(OpenMethod.RESUME, "none")
        if caps.load_session:
            # 只有 load：历史照样会被重放，标 origin=replay 送出，由 server 丢弃
            return Plan(OpenMethod.LOAD, "full")
    else:
        if caps.load_session:
            return Plan(OpenMethod.LOAD, "full")
        if caps.resume:
            # 只支持 resume 的 v1 agent 无法重放：恢复上下文，但如实报告没有重放
            return Plan(OpenMethod.RESUME, "none")
    return Plan(OpenMethod.NEW, "none")
