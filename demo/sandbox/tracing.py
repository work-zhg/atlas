"""输出：每行带上「是哪一层在说话」，按列对齐。

一条命令要穿过好几层：模型 → 图里的 execute 工具 → server 的 PodSandbox → cluster / K8s →
Pod 里的 sandboxd → 屏障 → 挂载器 → 对象存储。每层一个固定的标签，读日志时一眼看出是谁。

★ 行首的 [r1] 是当前 run：server 侧存在 contextvar 里，asyncio 任务会继承。
  sandboxd 跑在线程里（真实的它在另一个 Pod 里），看不到 run，只认 exec_id —— 真实系统也是这样。
★ sandboxd 的线程和 asyncio 同时在打印，写一行要加锁，否则两行会搅在一起。
"""

from __future__ import annotations

import contextvars
import sys
import threading

_run: contextvars.ContextVar[str] = contextvars.ContextVar("run", default="")
_lock = threading.Lock()


def enter(run_id: str) -> None:
    _run.set(run_id)


def _width(text: str) -> int:
    return sum(2 if ord(c) > 127 else 1 for c in text)  # 中文占两列


def say(who: str, message: str) -> None:
    run = _run.get()
    head = f"[{run}]" if run else ""
    line = f"{head:<5}{who}{' ' * (11 - _width(who))}│ {message}\n"
    with _lock:
        sys.stdout.write(line)
        sys.stdout.flush()
