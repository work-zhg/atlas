"""输出：每行带上「是哪一层在说话」，按列对齐。

一条命令要穿过好几层：模型 → 图里的 execute 工具 → server 的 PodSandbox / 路由 → OpenSandbox 的
lifecycle server → 控制器 → Pod 里的 execd → CSI 挂载 → 对象存储。每层一个固定的标签。

★ 行首的 [r1] 是当前 run：server 侧存在 contextvar 里，asyncio 任务会继承。
  OpenSandbox 和 Pod 里的代码看不到 run —— 真实系统也是这样，它们只认沙箱 id 和命令 id。
"""

from __future__ import annotations

import contextvars
import sys

_run: contextvars.ContextVar[str] = contextvars.ContextVar("run", default="")


def enter(run_id: str) -> None:
    _run.set(run_id)


def _width(text: str) -> int:
    return sum(2 if ord(c) > 127 else 1 for c in text)  # 中文占两列


def say(who: str, message: str) -> None:
    run = _run.get()
    head = f"[{run}]" if run else ""
    sys.stdout.write(f"{head:<5}{who}{' ' * (13 - _width(who))}│ {message}\n")
    sys.stdout.flush()
