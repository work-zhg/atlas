"""TurnLedger：最近若干轮的结果（Bridge 设计 §5.4）。

让 turn.start 可以安全重试：同一个 turnId 再次提交时，已结束的返回「已接纳」并重发一次
它的 ended 状态。更早的轮次不再记忆 —— 那时 server 早已入库，重试这么老的轮次本身就是错误。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from atlas_host import Outcome, TurnStats

__all__ = ["TurnLedger", "TurnRecord"]


@dataclass(frozen=True, slots=True)
class TurnRecord:
    outcome: Outcome
    stats: TurnStats


class TurnLedger:
    def __init__(self, size: int = 16) -> None:
        self._size = size
        self._records: OrderedDict[str, TurnRecord] = OrderedDict()

    def record(self, turn_id: str, record: TurnRecord) -> None:
        self._records[turn_id] = record
        self._records.move_to_end(turn_id)
        while len(self._records) > self._size:
            self._records.popitem(last=False)

    def get(self, turn_id: str) -> TurnRecord | None:
        return self._records.get(turn_id)

    def __contains__(self, turn_id: object) -> bool:
        return turn_id in self._records

    def __len__(self) -> int:
        return len(self._records)
