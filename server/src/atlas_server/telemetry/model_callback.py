"""模型调用的 span —— LangChain 回调（可观测性设计 §04 的 llm.chat）。

★ 为什么不从事件流派生：事件流里没有「模型调用开始/结束」这回事，
  message.delta 只是 token 增量，usage.updated 是**整轮**的累计值。
  要拿到单次调用的耗时与用量，只能从模型自己那层取。

★ 为什么挂在 default_model_builder：全部模型构造都过那一个函数 ——
  主对话（assembly.py:154）、标题生成（:238）、上下文摘要（:267）。
  挂在那里，三处一并覆盖，而且不用碰 runner（它是纯计算层）。

★ 只记元数据，不记内容（§08）。提示词与回答默认不进遥测管道：那里面
  有用户的私有代码与密钥，一旦进去，留存期与访问控制就跟业务数据脱钩了。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from . import semconv as sc

if TYPE_CHECKING:
    from langchain_core.outputs import LLMResult

logger = logging.getLogger(__name__)

__all__ = ["ModelSpanHandler"]


class ModelSpanHandler(BaseCallbackHandler):
    """每次模型调用产出一个 `chat` span。

    LangChain 用 run_id（它自己的，与我们的 run 无关）配对 start/end，
    这里用同一个键索引未结束的 span。
    """

    def __init__(self) -> None:
        self._spans: dict[UUID, Any] = {}

    # ------------------------------------------------------------------ 开始

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: Any, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._start(run_id, kwargs)

    def on_llm_start(
        self, serialized: dict[str, Any], prompts: list[str], *, run_id: UUID, **kwargs: Any
    ) -> None:
        # 聊天模型走 on_chat_model_start；这条是补给非聊天模型的，同样处理。
        self._start(run_id, kwargs)

    def _start(self, run_id: UUID, kwargs: dict[str, Any]) -> None:
        try:
            from opentelemetry import trace as ot

            from . import tracer

            params = kwargs.get("invocation_params") or {}
            model = str(params.get("model") or params.get("model_name") or "")
            # ★ 不传 context：让它挂在**当前上下文**的 span 上。模型调用发生在
            #   RunTrace 的根 span 之内（同一个 task），所以父子关系天然成立。
            self._spans[run_id] = tracer().start_span(
                f"chat {model}".strip(),
                kind=ot.SpanKind.CLIENT,
                attributes={
                    sc.OPERATION_NAME: sc.OP_CHAT,
                    sc.REQUEST_MODEL: model,
                },
            )
        except Exception:
            logger.debug("模型 span 开启失败", exc_info=True)

    # ------------------------------------------------------------------ 结束

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **_kw: Any) -> None:
        span = self._spans.pop(run_id, None)
        if span is None:
            return
        try:
            self._fill_usage(span, response)
        except Exception:
            logger.debug("模型 span 用量填充失败", exc_info=True)
        finally:
            span.end()

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **_kw: Any) -> None:
        span = self._spans.pop(run_id, None)
        if span is None:
            return
        try:
            from opentelemetry.trace import Status, StatusCode

            span.set_status(Status(StatusCode.ERROR, str(error)))
            span.set_attribute("error.type", type(error).__name__)
        finally:
            span.end()

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _fill_usage(span: Any, response: LLMResult) -> None:
        """用量与 finish_reason。

        ★ 复用 translator.normalize_usage —— 它已经把缓存与推理 token 拆成
          独立字段了（网关实测口径），这里不要另写一份解析：两份解析必然
          在某次上游变更后漂移，而漂移的表现是成本报表悄悄对不上。
        """
        from ..domain.translator import normalize_usage

        generations = [g for batch in (response.generations or []) for g in batch]
        message = getattr(generations[0], "message", None) if generations else None

        usage = normalize_usage(getattr(message, "usage_metadata", None))
        for attr, key in (
            (sc.USAGE_INPUT, "input_tokens"),
            (sc.USAGE_OUTPUT, "output_tokens"),
            (sc.USAGE_CACHE_READ, "cache_read"),
            (sc.USAGE_CACHE_CREATION, "cache_creation"),
            (sc.USAGE_REASONING, "thinking_tokens"),
        ):
            if isinstance(usage.get(key), int):
                span.set_attribute(attr, usage[key])

        meta = getattr(message, "response_metadata", None) or {}
        if model := meta.get("model_name") or meta.get("model"):
            span.set_attribute(sc.RESPONSE_MODEL, str(model))
        if stop := meta.get("stop_reason") or meta.get("finish_reason"):
            # semconv 规定它是**数组** —— 一次响应可能有多个候选
            span.set_attribute(sc.FINISH_REASONS, [str(stop)])
