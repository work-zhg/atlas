"""NodeWork：流程运行对「节点干活」的全部要求，由节点协作实现（扩展点，倒置依赖）。

都在调用方的事务里排队，事务提交后才真正调 Agent 平台；流程运行不认识 Agent 平台。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .models import ProcessNode

__all__ = ["NodeWork", "WorkOrder"]


@dataclass(frozen=True)
class WorkOrder:
    """给数字员工的一轮工作单（结构化的；怎么措辞由节点协作决定）。"""

    node_name: str
    instructions: str
    round: int
    #: 上游最近一次提交：[(节点名, [路径])]
    upstream: list[tuple[str, list[str]]] = field(default_factory=list)
    #: 驳回或打回的意见
    feedback: str | None = None


class NodeWork(Protocol):
    async def kickoff(self, node: ProcessNode, order: WorkOrder) -> None:
        """开工或下一轮：排一条指令，提交后投递。"""

    async def freeze(self, node: ProcessNode) -> None:
        """评审期间冻结：不再开 run，提交后取消最后开的 run。"""

    async def stop(self, nodes: list[ProcessNode]) -> None:
        """停下（被打回、流程终止）：排着的指令取消，提交后取消最后开的 run。"""
