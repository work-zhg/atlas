"""模型调用的观察者：遥测在 server 那边，这里只把框架的回调翻成中立的三件事。

★ 为什么不从事件流派生：事件流里没有「模型调用开始/结束」这回事，message.delta 只是 token 增量，
  usage.updated 是**整轮**的累计值。要拿到单次调用的耗时与用量，只能从模型自己那层取。
★ 挂在模型的构造器上（build_chat_model 的 observers）：主对话、标题生成、上下文摘要、自动模式
  判断器全部经过它，一处挂上处处覆盖，而且不用碰 runner。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from atlas_engine.runtime.translator import normalize_usage

__all__ = ["ModelObserver", "callback_for"]

logger = logging.getLogger(__name__)


class ModelObserver(Protocol):
    """一次模型调用的开始、结束、失败。call_id 配对开始和结束（框架自己的调用号，与 run 无关）。

    messages / message 是展示用的形状：{role, content[, tool_calls]}。不把框架的消息对象交出去。
    """

    def started(
        self, call_id: UUID, *, model: str, messages: list[dict[str, Any]] | None
    ) -> None: ...

    def finished(
        self,
        call_id: UUID,
        *,
        usage: Mapping[str, int],
        message: dict[str, Any] | None,
        response_model: str | None,
        stop_reason: str | None,
    ) -> None: ...

    def failed(self, call_id: UUID, error: BaseException) -> None: ...


class _ObserverCallback(BaseCallbackHandler):
    def __init__(self, observer: ModelObserver) -> None:
        self._observer = observer

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: Any, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._observer.started(run_id, model=_model_of(kwargs), messages=_messages_of(messages))

    def on_llm_start(
        self, serialized: dict[str, Any], prompts: list[str], *, run_id: UUID, **kwargs: Any
    ) -> None:
        # 聊天模型走 on_chat_model_start；这条是补给非聊天模型的，同样处理
        self._observer.started(run_id, model=_model_of(kwargs), messages=None)

    def on_llm_end(self, response: Any, *, run_id: UUID, **_kw: Any) -> None:
        generations = [g for batch in (response.generations or []) for g in batch]
        message = getattr(generations[0], "message", None) if generations else None
        meta = getattr(message, "response_metadata", None) or {}
        model = meta.get("model_name") or meta.get("model")
        stop = meta.get("stop_reason") or meta.get("finish_reason")
        self._observer.finished(
            run_id,
            # ★ 复用 normalize_usage：缓存与推理 token 已拆成独立字段（网关实测口径）。另写一份
            #   解析必然在某次上游变更后漂移，表现是成本报表悄悄对不上
            usage=normalize_usage(getattr(message, "usage_metadata", None)),
            message=_message_of(message),
            response_model=str(model) if model else None,
            stop_reason=str(stop) if stop else None,
        )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **_kw: Any) -> None:
        self._observer.failed(run_id, error)


def callback_for(observer: ModelObserver) -> BaseCallbackHandler:
    """观察者 → 框架的回调（build_chat_model 挂在构造器上）。"""
    return _ObserverCallback(observer)


def _model_of(kwargs: Mapping[str, Any]) -> str:
    params = kwargs.get("invocation_params") or {}
    return str(params.get("model") or params.get("model_name") or "")


def _message_of(message: Any) -> dict[str, Any] | None:
    """框架的消息 → {role, content[, tool_calls]}。只取展示需要的字段。"""
    if message is None:
        return None
    out: dict[str, Any] = {
        "role": getattr(message, "type", "unknown"),
        "content": getattr(message, "content", ""),
    }
    if tool_calls := getattr(message, "tool_calls", None):
        out["tool_calls"] = [
            {"name": c.get("name"), "args": c.get("args")}
            for c in tool_calls
            if isinstance(c, dict)
        ]
    return out


def _messages_of(messages: Any) -> list[dict[str, Any]] | None:
    """on_chat_model_start 的 messages 是「批次 × 消息」的二维列表；只记第一批。"""
    if not messages or not isinstance(messages, list):
        return None
    batch = messages[0] if isinstance(messages[0], list) else messages
    return [m for m in (_message_of(x) for x in batch) if m is not None]
