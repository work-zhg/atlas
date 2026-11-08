"""模型调用的 span（可观测性设计 §04 的 llm.chat）。

engine 的模型构造器把框架的回调翻成三件事（atlas_engine/runtime/observe.py 的 ModelObserver）：
开始、结束、失败。这里把它们记成 OTel span —— 不认识 LangChain。

★ 为什么挂在 default_model_builder：全部模型构造都过那一个函数 —— 主对话、标题生成、
  上下文摘要、自动模式判断器。挂在那里一并覆盖，而且不用碰 runner（它是纯计算层）。

★ 默认只记元数据（§08）。提示词与回答只在内容档位为 full 时记录，且经过脱敏和截断
  （telemetry/content.py）：那里面有用户的私有代码与密钥，一旦进了遥测管道，
  留存期与访问控制就跟业务数据脱钩了。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from . import content
from . import semconv as sc

logger = logging.getLogger(__name__)

__all__ = ["ModelSpanObserver"]


class ModelSpanObserver:
    """每次模型调用产出一个 `chat` span（实现 engine 的 ModelObserver）。"""

    def __init__(self) -> None:
        self._spans: dict[UUID, Any] = {}

    def started(self, call_id: UUID, *, model: str, messages: list[dict[str, Any]] | None) -> None:
        try:
            from opentelemetry import trace as ot

            from . import tracer

            # ★ 不传 context：让它挂在**当前上下文**的 span 上。模型调用发生在
            #   RunTrace 的根 span 之内（同一个 task），所以父子关系天然成立。
            self._spans[call_id] = tracer().start_span(
                f"chat {model}".strip(),
                kind=ot.SpanKind.CLIENT,
                attributes={
                    sc.OPERATION_NAME: sc.OP_CHAT,
                    sc.REQUEST_MODEL: model,
                    # ★ 显式标成模型调用：只有它带 gen_ai.usage.*，计费以它为准
                    sc.LF_OBSERVATION_TYPE: sc.OBS_GENERATION,
                },
            )
            # full 档：这次调用的完整输入（系统提示词 + 历史 + 本轮）
            if (text := content.render(messages, "full")) is not None:
                self._spans[call_id].set_attribute(sc.LF_OBSERVATION_INPUT, text)
        except Exception:
            logger.debug("模型 span 开启失败", exc_info=True)

    def finished(
        self,
        call_id: UUID,
        *,
        usage: Mapping[str, int],
        message: dict[str, Any] | None,
        response_model: str | None,
        stop_reason: str | None,
    ) -> None:
        span = self._spans.pop(call_id, None)
        if span is None:
            return
        try:
            for attr, key in (
                (sc.USAGE_INPUT, "input_tokens"),
                (sc.USAGE_OUTPUT, "output_tokens"),
                (sc.USAGE_CACHE_READ, "cache_read"),
                (sc.USAGE_CACHE_CREATION, "cache_creation"),
                (sc.USAGE_REASONING, "thinking_tokens"),
            ):
                if isinstance(usage.get(key), int):
                    span.set_attribute(attr, usage[key])
            if (text := content.render(message, "full")) is not None:
                span.set_attribute(sc.LF_OBSERVATION_OUTPUT, text)
            if response_model:
                span.set_attribute(sc.RESPONSE_MODEL, response_model)
            if stop_reason:
                # semconv 规定它是**数组** —— 一次响应可能有多个候选
                span.set_attribute(sc.FINISH_REASONS, [stop_reason])
        except Exception:
            logger.debug("模型 span 用量填充失败", exc_info=True)
        finally:
            span.end()

    def failed(self, call_id: UUID, error: BaseException) -> None:
        span = self._spans.pop(call_id, None)
        if span is None:
            return
        try:
            from opentelemetry.trace import Status, StatusCode

            span.set_status(Status(StatusCode.ERROR, str(error)))
            span.set_attribute("error.type", type(error).__name__)
        finally:
            span.end()
