"""每次模型调用时追加的 <system-reminder>（c4-model/agent/native-runtime/code-native-runtime-context.html）。

提醒的内容由各自的来源给（记忆的更新提示由记忆组件现取，待办提醒由 TodoList 给，用量提醒由
上下文流水线按档位给），这里只负责追加和控制总量，不读别的领域的 state 字段。只进这一次请求，
不落库、不进历史，提示词缓存的前缀不受影响。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langchain_core.messages import HumanMessage

from atlas_engine.kernel.context._text import estimate_text

__all__ = ["ReminderSource", "Reminders"]

#: 一个提醒来源：看图 state，给出要追加的 (标签, 正文)，没有就给空
ReminderSource = Callable[[Mapping[str, Any]], Sequence[tuple[str, str]]]


class Reminders:
    """按顺序向各来源要提醒，在 budget_tokens 内取；超出预算的那条截断，之后的不再追加。"""

    def __init__(
        self,
        *,
        budget_tokens: int,
        sources: Sequence[ReminderSource] = (),
        static: Sequence[tuple[str, str]] = (),
    ) -> None:
        self._budget = budget_tokens
        self._sources = list(sources)
        self._static = list(static)

    def collect(
        self, state: Mapping[str, Any], *, extra: Sequence[tuple[str, str]] = ()
    ) -> list[tuple[str, str]]:
        """→ 这次调用要追加的提醒。extra 是流水线自己的（用量提醒），排在最后。"""
        out: list[tuple[str, str]] = list(self._static)
        for source in self._sources:
            out.extend(source(state))
        out.extend(extra)
        picked: list[tuple[str, str]] = []
        budget = self._budget
        for name, text in out:
            cost = estimate_text(text)
            if cost > budget:
                text = text[: int(budget * 2.5)] + "\n…（附件超出预算已截断）"
                cost = budget
            if cost <= 0:
                break
            picked.append((name, text))
            budget -= cost
        return picked

    @staticmethod
    def attach(request: Any, reminders: Sequence[tuple[str, str]]) -> Any:
        """把提醒作为最后一条消息追加到这次请求上（不改 state）。"""
        if not reminders:
            return request
        body = "\n\n".join(
            f'<system-reminder name="{n}">\n{t}\n</system-reminder>' for n, t in reminders
        )
        note = "（以下为平台附加的上下文，不是用户说的话）\n" + body
        return request.override(messages=[*request.messages, HumanMessage(content=note)])
