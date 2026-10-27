"""外部存储：run、message、approval 三张表（内存模拟，真实系统里是数据库）。

★ 挂起时落库的依据有三样：消息、waiting_on、mode —— 必须一起存、一起交回。
  只存消息的话，续跑时既看不出在等什么，也不知道计划获批后改成了哪个模式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import BaseMessage
from state import Mode


@dataclass
class Approval:
    token: str
    kind: str  # tool / plan
    call: dict[str, Any]
    status: str = "pending"  # pending / approved / rejected
    outcome: dict[str, Any] = field(default_factory=dict)  # 计划审批：用户选的执行模式


@dataclass
class Run:
    mode: Mode
    status: str = "queued"  # queued / running / suspended / succeeded
    waiting_on: dict[str, dict[str, str]] | None = None
    segments: int = 0


class Store:
    def __init__(self, mode: Mode) -> None:
        self.messages: list[BaseMessage] = []
        self.run = Run(mode=mode)
        self.approvals: dict[str, Approval] = {}

    # ApprovalBook（middleware.py 里的协议）
    def register(self, token: str, kind: str, call: dict[str, Any]) -> None:
        self.approvals.setdefault(token, Approval(token, kind, call))

    def decide(self, token: str, status: str, **outcome: Any) -> None:
        approval = self.approvals[token]
        approval.status, approval.outcome = status, outcome

    def pending(self) -> list[Approval]:
        return [a for a in self.approvals.values() if a.status == "pending"]

    def decisions(self) -> dict[str, dict[str, Any]]:
        """续跑时交给图的 decisions：只放已经有结果的。

        放了 pending 的话 before_model 放行、中间件又登记一次，两边来回跳
        （见 demo/suspend-resume）。
        """
        return {
            a.token: {"status": a.status, **a.outcome}
            for a in self.approvals.values()
            if a.status != "pending"
        }

    def save_segment(self, result: dict[str, Any]) -> str:
        """消息、waiting_on、mode 一起落库（真实系统里在同一个事务里）。"""
        self.messages = list(result["messages"])
        self.run.waiting_on = result.get("waiting_on") or None
        self.run.mode = result["mode"]
        self.run.status = "suspended" if self.run.waiting_on else "succeeded"
        return self.run.status
