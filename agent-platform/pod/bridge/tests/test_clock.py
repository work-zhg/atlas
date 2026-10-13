"""Timer 与 FakeClock（代码设计 §7.10）。"""

from __future__ import annotations

from atlas_bridge.clock import Timer
from atlas_bridge.testing.clock import FakeClock


def make() -> tuple[FakeClock, Timer, list[float]]:
    clock = FakeClock()
    fired: list[float] = []
    return clock, Timer(clock, lambda: fired.append(clock.now()), name="t"), fired


def test_fires_once_at_deadline() -> None:
    clock, timer, fired = make()
    timer.start(10)
    clock.advance(9.9)
    assert fired == [] and timer.running
    clock.advance(0.2)
    assert fired == [10] and not timer.running
    clock.advance(100)
    assert fired == [10]


def test_reset_restarts_with_same_duration() -> None:
    """静默计时：每收到一次进展就重新开始。"""
    clock, timer, fired = make()
    timer.start(10)
    clock.advance(8)
    timer.reset()
    clock.advance(8)
    assert fired == []
    clock.advance(2)
    assert fired == [18]


def test_reset_with_new_duration() -> None:
    """静默阈值在「等模型」与「执行工具」之间切换时，用新时长重新开始。"""
    clock, timer, fired = make()
    timer.start(120)
    clock.advance(100)
    timer.reset(600)
    clock.advance(500)
    assert fired == []
    clock.advance(100)
    assert fired == [700]


def test_pause_keeps_remaining_and_resume_continues() -> None:
    """等人时静默计时暂停，答复后从剩余时间继续。"""
    clock, timer, fired = make()
    timer.start(10)
    clock.advance(4)
    timer.pause()
    assert timer.paused and timer.remaining == 6
    clock.advance(1000)
    assert fired == []
    timer.resume()
    clock.advance(5.9)
    assert fired == []
    clock.advance(0.2)
    assert fired == [1010]


def test_reset_while_paused_unpauses() -> None:
    clock, timer, fired = make()
    timer.start(10)
    timer.pause()
    timer.reset()
    assert timer.running and not timer.paused
    clock.advance(10)
    assert fired == [10]


def test_stop_cancels_everything() -> None:
    clock, timer, fired = make()
    timer.start(10)
    timer.stop()
    clock.advance(20)
    assert fired == [] and timer.remaining is None
    timer.start(5)
    timer.pause()
    timer.stop()
    timer.resume()  # 已停止：什么都不发生
    clock.advance(20)
    assert fired == []


def test_fake_clock_fires_in_time_order_including_callbacks_scheduled_during_advance() -> None:
    clock = FakeClock()
    seen: list[str] = []
    clock.call_later(5, lambda: seen.append("b"))
    clock.call_later(1, lambda: (seen.append("a"), clock.call_later(2, lambda: seen.append("a2"))))
    clock.advance(10)
    assert seen == ["a", "a2", "b"]
