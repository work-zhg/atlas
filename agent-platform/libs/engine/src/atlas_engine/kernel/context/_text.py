"""上下文管理各策略共用的小工具：取消息文本、粗估 token、发事件。

刻意只依赖 langchain_core 的消息类型 —— 各策略都能脱离图单独测。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import BaseMessage

logger = logging.getLogger(__name__)

#: 粗略 token 估算的字符除数（中文约 1 字 1 token、英文约 4 字符 1 token，取偏保守的 2.5）
CHARS_PER_TOKEN = 2.5

#: 摘要回填时的前缀。重建历史（server）和构成统计都靠它认出摘要消息
SUMMARY_PREFIX = "【以下是早期对话的摘要】\n"


def text_of(message: BaseMessage) -> str:
    """估算用的文本：文本块，加上工具结果块的 content、tool_use 块的 input。"""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                for key in ("text", "content", "input"):
                    if key in block:
                        parts.append(str(block[key]))
        return "".join(parts)
    return str(content)


def plain_text(message: BaseMessage) -> str:
    """只取文本块（图片等多模态块不算）—— 外置写进文件的就是这一份。"""
    content = message.content
    if isinstance(content, str):
        return content
    return "\n".join(
        str(b.get("text", "")) if isinstance(b, dict) else str(b)
        for b in content or []
        if isinstance(b, str) or (isinstance(b, dict) and b.get("type") == "text")
    )


def estimate_text(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def estimate_tokens(messages: Sequence[BaseMessage]) -> int:
    """粗估一批消息的 tokens。tool_use 的入参也算进来（往往是大头）。"""
    total = 0
    for m in messages:
        total += len(text_of(m))
        for call in getattr(m, "tool_calls", None) or []:
            total += len(json.dumps(call.get("args") or {}, ensure_ascii=False, default=str))
    return int(total / CHARS_PER_TOKEN)


def emit(payload: dict[str, Any]) -> None:
    """经 custom stream 发一条上下文事件（context.usage / cleared / compacted）。"""
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(payload)
    except Exception:  # 图外调用（单测）时没有 writer
        logger.debug("无 stream writer，跳过 %s 事件", payload.get("kind"))
