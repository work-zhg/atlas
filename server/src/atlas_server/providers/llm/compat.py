"""第三方兼容补丁。

只放"上游 bug / 网关怪癖"的最小修补，每个补丁必须写清：
触发条件、影响版本、以及上游修好后如何自动失效。

── 补丁 1：litellm × langchain-anthropic 流式崩溃 ──────────────────────────
现象：任何走 litellm 网关的 `astream()` 调用抛
      AttributeError: 'dict' object has no attribute 'model_dump'

成因：litellm 在 `message_delta` 事件里回传 `context_management` 字段；
      anthropic SDK 至今（0.122 / 0.123 均验证过）未定义该字段，
      而其模型是 extra="allow"，于是它以**普通 dict** 落在模型上；
      langchain-anthropic 假设它是 pydantic 模型，直接调用 `.model_dump()`。

      该分支被 `and stream_usage` 保护，因此关掉 stream_usage 也能规避，
      但那样就拿不到 token 计数（usage.updated 事件需要），不可接受。

★ 接缝随大版本变了（P4 升级时实测）：
      · langchain-anthropic 0.3.x：**模块级函数**
        chat_models._make_message_chunk_from_anthropic_event
      · langchain-anthropic 1.5.x：**类方法**
        ChatAnthropic._make_message_chunk_from_anthropic_event
      两处都打，谁在就打谁；都不在则告警（不静默失败）。

修补：把 dict 包一层带 `model_dump()` 的 dict 子类。不改变任何语义，
      仅让上游的鸭子类型断言成立。

失效条件：一旦 anthropic SDK 定义了该字段（届时它是真的 pydantic 模型），
          isinstance(dict) 判定为假，补丁自动变成 no-op。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False
_METHOD = "_make_message_chunk_from_anthropic_event"


class _CompatDict(dict):
    """带 model_dump() 的 dict —— 让上游的 `.model_dump()` 调用成立。"""

    def model_dump(self, *_: Any, **__: Any) -> dict:
        return dict(self)


def _normalize(event: Any) -> None:
    cm = getattr(event, "context_management", None)
    if isinstance(cm, dict) and not hasattr(cm, "model_dump"):
        # 模型被冻结时退让，不影响主流程
        with contextlib.suppress(AttributeError, ValueError):
            event.context_management = _CompatDict(cm)


def apply_litellm_compat() -> None:
    """幂等。model_factory 在 import 时调用一次即可。"""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    try:
        from langchain_anthropic import ChatAnthropic, chat_models
    except ImportError:  # engine 的纯度测试里不装 langchain 也应能通过
        return

    patched_any = False

    # 1.5.x：类方法
    method = getattr(ChatAnthropic, _METHOD, None)
    if method is not None:

        def patched_method(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
            _normalize(event)
            return method(self, event, *args, **kwargs)

        setattr(ChatAnthropic, _METHOD, patched_method)
        patched_any = True

    # 0.3.x：模块级函数
    func = getattr(chat_models, _METHOD, None)
    if func is not None and not hasattr(ChatAnthropic, _METHOD):

        def patched_func(event: Any, *args: Any, **kwargs: Any) -> Any:
            _normalize(event)
            return func(event, *args, **kwargs)

        chat_models._make_message_chunk_from_anthropic_event = patched_func  # type: ignore[attr-defined]
        patched_any = True

    if not patched_any:
        # 不静默失败：上游又改了结构，留下痕迹便于定位
        logger.warning(
            "langchain-anthropic 的 %s 接缝已不存在，litellm 兼容补丁未生效；"
            "若流式调用报 model_dump 错误，请检查此处",
            _METHOD,
        )
    else:
        logger.debug("applied litellm×langchain-anthropic streaming compat patch")
