"""ProgressTracker：进行中的工具调用与静默阈值（Bridge 设计 §6.2 · 代码设计 §7.6）。

只读 ``sessionUpdate``、``toolCallId``、``status`` 三个字段，不解析整条 update。
"""

from __future__ import annotations

from typing import Any

from atlas_acp.v1 import tool_status

__all__ = ["ProgressTracker"]


class ProgressTracker:
    def __init__(self, *, idle_s: float, tool_idle_s: float) -> None:
        self._idle_s = idle_s
        self._tool_idle_s = tool_idle_s
        self.in_progress_tools: set[str] = set()

    def observe(self, raw_update: dict[str, Any]) -> None:
        status = tool_status(raw_update)
        if status is None:
            return
        # ★ 出现过、还没到终态的都算在执行。只认 in_progress 的话，适配器若从 pending
        #   直接跳到 completed（不报 in_progress），长命令就只有 idleS 的阈值，会被当成卡死
        if status.finished:
            self.in_progress_tools.discard(status.tool_call_id)
        else:
            self.in_progress_tools.add(status.tool_call_id)

    @property
    def idle_threshold(self) -> float:
        """没有工具在执行 → 在等模型，用 idleS；否则允许长时间没有输出，用 toolIdleS。"""
        return self._tool_idle_s if self.in_progress_tools else self._idle_s
