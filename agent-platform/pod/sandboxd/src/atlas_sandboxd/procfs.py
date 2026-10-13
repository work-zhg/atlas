"""进程组的 CPU 时间（静默卡死判定用）。

★ 只看输出会误杀安静的长任务（编译、数据处理常常几分钟不打印）；
  「既没输出、CPU 时间也不涨」才算卡死。Linux 以外没有 /proc，返回 None，调用方退回只看输出。
"""

from __future__ import annotations

import os

__all__ = ["group_cpu_seconds"]

_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def group_cpu_seconds(pgid: int) -> float | None:
    if not os.path.isdir("/proc/self"):
        return None
    total = 0
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                raw = f.read().decode(errors="replace")
        except OSError:  # 进程刚退出
            continue
        # comm 可能含空格与括号：从最后一个 ')' 之后切
        fields = raw[raw.rfind(")") + 2 :].split()
        # fields[0] = state（第 3 个字段）；pgrp 第 5 → fields[2]；
        # utime / stime 第 14 / 15 → fields[11] / fields[12]
        try:
            if int(fields[2]) != pgid:
                continue
            total += int(fields[11]) + int(fields[12])
        except (IndexError, ValueError):
            continue
    return total / _TICKS
