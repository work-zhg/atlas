"""中间件顺序的唯一来源（c4-model/agent/native-runtime/code-native-runtime-turn.html）。

各领域交中间件时带上槽位，这里按槽位从外到内排好；同一槽位内保持交进来的先后。
提示词缓存不占槽位，永远在最内层（create_deep_agent 追加）：改系统提示词的中间件都在它
外面，缓存前缀才不会每轮失效。

    ① SUSPEND     挂起与续跑的支点，含图入口的补悬空调用 —— 要在任何「为模型调用做准备」
                  的工作之前跳出（挂起与人机协作）
    ② ASK         向用户提问：排在审批外层、自己处理自己的工具，「不想回答」不会被当成
                  「拒绝执行」（挂起与人机协作）
    ③ PERMISSION  审批：要在工具执行之前拦下（挂起与人机协作）
    ④ CONTEXT     上下文管理：清理、压缩要在别的中间件动手前拿到完整消息（上下文管理）
    ⑤ TOOLS       工具：技能清单、文件、Shell、工具搜索、待办（工具面）
    ⑥ LIMITS      限额：步数、工具并发、连续失败熔断（工具面）
    ⑦ SUBAGENTS   派子智能体（挂起与人机协作）

★ 限额与子智能体的先后和 C4 图上的编号相反：task 由子智能体中间件拦下、不往里调用，限额
  排在它里面的话，并发上限和连续失败熔断就管不到 task —— 与「限额配了就必须生效」冲突，
  也会改变现有行为（派子智能体连续失败时被熔断拦住）。这里限额在外。
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import IntEnum
from typing import Any


class Slot(IntEnum):
    SUSPEND = 1
    ASK = 2
    PERMISSION = 3
    CONTEXT = 4
    TOOLS = 5
    LIMITS = 6
    SUBAGENTS = 7


class MiddlewareStack:
    """按槽位收中间件，ordered() 给出从外到内的顺序。"""

    def __init__(self) -> None:
        self._entries: list[tuple[Slot, int, Any]] = []

    def add(self, slot: Slot, middleware: Any) -> MiddlewareStack:
        self._entries.append((slot, len(self._entries), middleware))
        return self

    def extend(self, slot: Slot, middlewares: Iterable[Any]) -> MiddlewareStack:
        for middleware in middlewares:
            self.add(slot, middleware)
        return self

    def merge(self, other: MiddlewareStack) -> MiddlewareStack:
        """把另一组交来的中间件并进来，各自的槽位不变、同槽位排在已有的后面。"""
        for slot, _, middleware in sorted(other._entries, key=lambda e: e[1]):
            self.add(slot, middleware)
        return self

    def ordered(self) -> list[Any]:
        return [m for _, _, m in sorted(self._entries, key=lambda e: (e[0], e[1]))]
