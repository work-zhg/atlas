"""输出：每行带上「是哪个 run 的哪一段在说话」，按嵌套深度缩进。

SubAgent 可以继续派 SubAgent，同一时刻有好几层 run 在跑，
图里的中间件和模型并不知道自己属于哪个 run。执行器在跑一段之前用 enter() 记下前缀；
它存在 contextvar 里，LangGraph 的并行任务和模型的线程池都会继承，
所以图里任何地方调用 say() 都能带上正确的前缀。
"""

from __future__ import annotations

import contextvars

_prefix: contextvars.ContextVar[str] = contextvars.ContextVar("prefix", default="")


def enter(run_id: str, segment: int) -> None:
    depth = run_id.count("-")  # r1 是根，r1-c1 是第 1 层，r1-c1-c1 是第 2 层
    _prefix.set(f"{'    ' * depth}[{run_id}·段{segment}] ")


def say(message: str) -> None:
    print(f"{_prefix.get()}{message}")
