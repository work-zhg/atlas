"""上下文的预算和档位（c4-model/agent/native-runtime/code-native-runtime-context.html）。

有效窗口 = 窗口 − min(最大输出, 20k)，给回复和摘要留位置；按占用分出清理、压缩、提醒三档。
占用优先用模型返回的实际用量，没有时按消息粗估（工具调用的入参也算）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from atlas_engine.kernel.context._text import (
    SUMMARY_PREFIX,
    estimate_text,
    estimate_tokens,
    text_of,
)

__all__ = ["Composition", "ContextBudget", "Level", "TokenMeter", "composition", "effective_window"]


class Level(IntEnum):
    """占用的档位。各档的阈值互相独立（见 ContextBudget.reached），这里的先后只用于展示。"""

    NORMAL = 0
    CLEAR = 1
    COMPACT = 2
    WARN = 3


@dataclass
class TokenMeter:
    """当前上下文占用的计量。

    ★ 以最近一次模型调用的 usage 为基线（input 已含 cache_read / cache_creation，output 是那条
      回复本身），加上它之后新增消息的估算。没有基线（新会话首轮、从库里重建的历史不带 usage）
      时退回整体估算 + 已观测到的固定开销（系统提示词 + 工具定义）。
    """

    overhead_tokens: int = 0

    def measure(self, messages: Sequence[BaseMessage]) -> tuple[int, str]:
        """→ (tokens, 口径)。口径：usage | estimate。"""
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            usage = getattr(m, "usage_metadata", None) if isinstance(m, AIMessage) else None
            if usage and usage.get("input_tokens"):
                base = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
                return base + estimate_tokens(messages[i + 1 :]), "usage"
        return estimate_tokens(messages) + self.overhead_tokens, "estimate"


def effective_window(context_window: int, max_output_tokens: int) -> int:
    """有效窗口 = 窗口 − min(最大输出, 20k)：给回复与压缩时的摘要输出留位置。"""
    return max(1, context_window - min(max_output_tokens, 20_000))


class ContextBudget:
    """一个模型的上下文预算。比例都相对有效窗口；None 的档位不启用。"""

    def __init__(
        self,
        *,
        context_window: int,
        compact_ratio: float,
        max_output_tokens: int = 0,
        clear_ratio: float | None = None,
        warn_ratio: float | None = None,
    ) -> None:
        self.window = context_window
        self.effective = effective_window(context_window, max_output_tokens)
        self.compact_at = int(self.effective * compact_ratio)
        self.clear_at = int(self.effective * clear_ratio) if clear_ratio else None
        self.warn_at = int(self.effective * warn_ratio) if warn_ratio else None
        self._meter = TokenMeter()

    def observe_overhead(self, tokens: int) -> None:
        """记下系统提示词 + 工具定义的开销：没有 usage 基线时估算要加上它。"""
        self._meter.overhead_tokens = tokens

    def measure(self, messages: Sequence[BaseMessage]) -> tuple[int, str]:
        return self._meter.measure(messages)

    def tokens(self, messages: Sequence[BaseMessage]) -> int:
        return self._meter.measure(messages)[0]

    def reached(self, level: Level, tokens: int) -> bool:
        """占用到没到某一档。★ 压缩档是「超过」，另两档是「达到」。"""
        if level is Level.COMPACT:
            return tokens > self.compact_at
        if level is Level.CLEAR:
            return self.clear_at is not None and tokens >= self.clear_at
        if level is Level.WARN:
            return self.warn_at is not None and tokens >= self.warn_at
        return True

    def level(self, tokens: int) -> Level:
        """到达的最高一档（展示用）。"""
        return max((lv for lv in Level if self.reached(lv, tokens)), default=Level.NORMAL)

    def thresholds(self) -> dict[str, int | None]:
        return {"clear": self.clear_at, "compact": self.compact_at, "warn": self.warn_at}


# ─────────────────────────────────────────────── 构成（context.usage 事件）


@dataclass
class Composition:
    system: int = 0
    tools: int = 0
    summary: int = 0
    conversation: int = 0
    tool_results: int = 0
    attachments: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)

    @property
    def total(self) -> int:
        return sum(self.__dict__.values())


def composition(
    *,
    system_text: str,
    tools: Sequence[Any],
    messages: Sequence[BaseMessage],
    attachment_tokens: int,
) -> Composition:
    c = Composition(system=estimate_text(system_text), attachments=attachment_tokens)
    for t in tools:
        try:
            from langchain_core.utils.function_calling import convert_to_openai_tool

            schema = t if isinstance(t, dict) else convert_to_openai_tool(t)
            c.tools += estimate_text(json.dumps(schema, ensure_ascii=False, default=str))
        except Exception:  # 某些工具无法转换 schema：按描述估
            c.tools += estimate_text(str(getattr(t, "description", "")))
    for m in messages:
        n = estimate_tokens([m])
        if isinstance(m, ToolMessage):
            c.tool_results += n
        elif isinstance(m, AIMessage) and text_of(m).startswith(SUMMARY_PREFIX):
            c.summary += n
        else:
            c.conversation += n
    return c
