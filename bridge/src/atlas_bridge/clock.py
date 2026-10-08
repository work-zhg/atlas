"""时钟与计时器（代码设计 §7.10）。

所有时限（截止、静默、取消宽限、询问过期）都经 ``Clock`` 创建，测试里换成假时钟，
推进 30 分钟只需一行，不必真的等待。时间一律用单调时钟（Bridge 设计 §6.7）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

__all__ = ["Clock", "SystemClock", "Timer", "TimerHandle"]


class TimerHandle(Protocol):
    def cancel(self) -> None: ...


class Clock(Protocol):
    def now(self) -> float:
        """单调时钟的当前读数（秒）。"""
        ...

    def call_later(self, delay: float, callback: Callable[[], None]) -> TimerHandle: ...


class SystemClock:
    """事件循环的单调时钟。"""

    def now(self) -> float:
        return asyncio.get_running_loop().time()

    def call_later(self, delay: float, callback: Callable[[], None]) -> TimerHandle:
        return asyncio.get_running_loop().call_later(max(delay, 0.0), callback)


class Timer:
    """可重置、可暂停的一次性计时器。

    · ``start`` 开始计时（已在计时则重新开始）
    · ``reset`` 按原时长（或新时长）重新开始 —— 静默计时每收到一次进展就调用它
    · ``pause`` / ``resume`` 暂停与继续，剩余时间保留 —— 等人时静默计时暂停
    · 到点只触发一次；触发、``stop`` 之后都回到空闲状态
    """

    def __init__(self, clock: Clock, callback: Callable[[], None], *, name: str = "") -> None:
        self._clock = clock
        self._callback = callback
        self.name = name
        self._duration: float | None = None
        self._deadline: float | None = None  # 计时中：到点时刻
        self._remaining: float | None = None  # 暂停中：剩余时长
        self._handle: TimerHandle | None = None

    # ------------------------------------------------------------------ 状态

    @property
    def running(self) -> bool:
        return self._deadline is not None

    @property
    def paused(self) -> bool:
        return self._remaining is not None

    @property
    def remaining(self) -> float | None:
        """剩余时长；空闲时为 None。"""
        if self._deadline is not None:
            return max(self._deadline - self._clock.now(), 0.0)
        return self._remaining

    # ------------------------------------------------------------------ 操作

    def start(self, duration: float) -> None:
        self._duration = duration
        self._arm(duration)

    def reset(self, duration: float | None = None) -> None:
        """重新开始。暂停中调用则重新开始计时并解除暂停。"""
        if duration is not None:
            self._duration = duration
        if self._duration is None:
            raise RuntimeError(f"计时器 {self.name!r} 从未 start 过，无法 reset")
        self._arm(self._duration)

    def pause(self) -> None:
        if self._deadline is None:
            return
        self._remaining = max(self._deadline - self._clock.now(), 0.0)
        self._disarm()

    def resume(self) -> None:
        if self._remaining is None:
            return
        remaining = self._remaining
        self._arm(remaining)

    def stop(self) -> None:
        self._disarm()
        self._remaining = None

    # ------------------------------------------------------------------ 内部

    def _arm(self, delay: float) -> None:
        self._disarm()
        self._remaining = None
        self._deadline = self._clock.now() + delay
        self._handle = self._clock.call_later(delay, self._fire)

    def _disarm(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self._deadline = None

    def _fire(self) -> None:
        self._handle = None
        self._deadline = None
        self._callback()
