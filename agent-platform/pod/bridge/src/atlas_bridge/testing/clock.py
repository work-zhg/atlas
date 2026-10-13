"""FakeClock：手动推进的假时钟。"""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = ["FakeClock"]


@dataclass(order=True)
class _Scheduled:
    when: float
    order: int
    callback: Callable[[], None] = field(compare=False)
    cancelled: bool = field(default=False, compare=False)

    def cancel(self) -> None:
        self.cancelled = True


class FakeClock:
    """``advance(seconds)`` 按时间顺序同步触发到期的回调。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._queue: list[_Scheduled] = []
        self._order = itertools.count()

    def now(self) -> float:
        return self._now

    def call_later(self, delay: float, callback: Callable[[], None]) -> _Scheduled:
        item = _Scheduled(self._now + max(delay, 0.0), next(self._order), callback)
        heapq.heappush(self._queue, item)
        return item

    def advance(self, seconds: float) -> None:
        target = self._now + seconds
        while self._queue and self._queue[0].when <= target:
            item = heapq.heappop(self._queue)
            if item.cancelled:
                continue
            self._now = item.when
            item.callback()  # 回调里可以再安排新的回调，同样会在本次推进中按时触发
        self._now = target

    @property
    def pending(self) -> int:
        return sum(1 for item in self._queue if not item.cancelled)
