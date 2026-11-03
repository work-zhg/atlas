"""模型调用失败时的补救（c4-model/agent/native-runtime/code-native-runtime-context.html）。

各只重试一次：上下文过长（按各家网关的措辞识别，只认 4xx）→ 强制压缩后重试；回复被输出上限
截断 → 调高输出上限重试。只试一次，避免在同一个错误上反复重试。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.messages import BaseMessage

from atlas_engine.kernel.context.compactor import Compactor

__all__ = ["CallRecovery", "is_context_overflow", "is_output_truncated"]

_OVERFLOW_HINTS = (
    "prompt is too long",
    "maximum context length",
    "context length",
    "context_length_exceeded",
    "context window",
    "too many tokens",
    "input is too long",
    "reduce the length",
)


def is_context_overflow(exc: BaseException) -> bool:
    """模型网关返回「上下文过长」—— 各家措辞不同，按关键词认。只认 4xx。"""
    status = getattr(exc, "status_code", None)
    if status is not None and not (isinstance(status, int) and 400 <= status < 500):
        return False
    text = str(exc).lower()
    return any(h in text for h in _OVERFLOW_HINTS)


def is_output_truncated(message: BaseMessage) -> bool:
    """回复因为达到输出上限被截断（Anthropic: stop_reason=max_tokens；OpenAI: finish_reason=length）。"""
    meta = getattr(message, "response_metadata", None) or {}
    return meta.get("stop_reason") == "max_tokens" or meta.get("finish_reason") == "length"


class CallRecovery:
    """compactor：上下文过长时用它强制压缩（None = 不补救）；max_output_cap：模型的输出上限
    （None = 截断了也不调高）。"""

    def __init__(self, *, compactor: Compactor | None = None, max_output_cap: int | None = None) -> None:
        self._compactor = compactor
        self._max_output_cap = max_output_cap

    async def shrink(
        self,
        error: BaseException,
        messages: list[BaseMessage],
        *,
        tokens: int,
        measure: Callable[[Sequence[BaseMessage]], int],
    ) -> list[BaseMessage] | None:
        """调用失败 → 压缩后用来重试的消息；不是上下文过长（或没法压缩）→ None，原样抛出。"""
        if self._compactor is None or not is_context_overflow(error):
            return None
        return await self._compactor.compact(messages, tokens=tokens, reason="overflow", measure=measure)

    def raise_output(self, request: Any, reply: BaseMessage | None) -> dict[str, Any] | None:
        """回复被输出上限截断 → 调高上限后的 model_settings；不用重试 → None。"""
        if reply is None or not self._max_output_cap or not is_output_truncated(reply):
            return None
        current = int(
            request.model_settings.get("max_tokens")
            or getattr(request.model, "max_tokens", 0)
            or 0
        )
        raised = min(self._max_output_cap, max(current * 2, 8_192))
        if raised <= current:
            return None
        return {**request.model_settings, "max_tokens": raised}
