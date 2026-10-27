"""输出：每行带上「哪个场景的哪一段在说话」。

执行器跑一段之前用 enter() 记下前缀；前缀存在 contextvar 里，
LangGraph 的工具任务和模型调用都会继承，
所以图里任何地方调用 say() 都能带上正确的前缀。
"""

from __future__ import annotations

import contextvars

_prefix: contextvars.ContextVar[str] = contextvars.ContextVar("prefix", default="")


def enter(scenario: str, segment: int) -> None:
    _prefix.set(f"  [{scenario}·段{segment}] ")


def say(message: str) -> None:
    print(f"{_prefix.get()}{message}")
