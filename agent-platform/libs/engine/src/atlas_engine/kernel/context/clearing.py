"""旧工具结果清理（c4-model/agent/native-runtime/code-native-runtime-context.html）。

占用到了清理档，或者距上次回复已超过提示词缓存的有效期（缓存反正失效了），就把较早的、
可清理工具的结果换成一句占位，最近 N 条保留。同 id 替换，不改消息顺序。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from atlas_engine.kernel.context._text import text_of

__all__ = ["CLEARED_PLACEHOLDER", "DEFAULT_CLEARABLE_TOOLS", "ResultClearing", "clear_plan"]

#: 可再生的工具：结果被清掉后，模型能用同样的调用重新拿回来。
#: ★ 写类工具（write_file / edit_file）的结果本来就短，清了也省不了多少，不在默认清单里。
#: ★ tool_search 不清：已加载的延迟工具是从它的结果里推出来的，清了工具就「消失」了。
DEFAULT_CLEARABLE_TOOLS = frozenset(
    {"read_file", "search", "execute", "knowledge_search", "search_memory"}
)

CLEARED_PLACEHOLDER = "[旧的工具结果已清除以节省上下文；需要时请重新调用 {name} 获取]"


def _call_names(messages: Sequence[BaseMessage]) -> dict[str, str]:
    """tool_call_id → 工具名（ToolMessage.name 有时为空，以发起调用的 AIMessage 为准）。"""
    out: dict[str, str] = {}
    for m in messages:
        for call in getattr(m, "tool_calls", None) or []:
            if call.get("id"):
                out[str(call["id"])] = str(call.get("name") or "")
    return out


def clear_plan(
    messages: Sequence[BaseMessage],
    *,
    clearable: Iterable[str],
    keep_recent: int,
) -> list[ToolMessage]:
    """要清掉的旧工具结果 → 替换后的 ToolMessage（同 id，按 id 去重的 reducer 会原地替换）。

    ★ 最后一批（最近一条 AIMessage 之后的）结果从不清：模型马上就要读它们。
    ★ 已经是占位的不重复清。
    """
    clearable = set(clearable)
    names = _call_names(messages)
    last_ai = max((i for i, m in enumerate(messages) if isinstance(m, AIMessage)), default=-1)
    candidates: list[ToolMessage] = []
    for i, m in enumerate(messages):
        if i > last_ai or not isinstance(m, ToolMessage) or not m.id:
            continue
        name = names.get(str(m.tool_call_id)) or (m.name or "")
        if name not in clearable:
            continue
        if text_of(m).startswith("[旧的工具结果已清除"):
            continue
        candidates.append(m)
    victims = candidates[: max(0, len(candidates) - keep_recent)]
    return [
        ToolMessage(
            content=CLEARED_PLACEHOLDER.format(
                name=names.get(str(m.tool_call_id)) or m.name or "该工具"
            ),
            tool_call_id=m.tool_call_id,
            name=m.name,
            id=m.id,
            status=getattr(m, "status", "success"),
        )
        for m in victims
    ]


class ResultClearing:
    """清理策略：什么时候清（due）、清哪些（plan）。

    cache_expired：本段开始时距上次回复已超过提示词缓存的有效期 —— 缓存反正失效了，第一次
    模型调用前顺手清一次，不等占用到清理档。只在第一次判断，之后的调用缓存是热的。
    """

    def __init__(
        self,
        *,
        keep_recent: int = 5,
        clearable: Iterable[str] = DEFAULT_CLEARABLE_TOOLS,
        cache_expired: bool = False,
    ) -> None:
        self._keep_recent = keep_recent
        self._clearable = frozenset(clearable)
        self._idle = cache_expired

    def due(self, *, at_clear_level: bool) -> str | None:
        """→ 清理的原因（threshold | idle），不用清时 None。"""
        idle, self._idle = self._idle, False
        if at_clear_level:
            return "threshold"
        return "idle" if idle else None

    def plan(self, messages: Sequence[BaseMessage]) -> list[ToolMessage]:
        return clear_plan(messages, clearable=self._clearable, keep_recent=self._keep_recent)
