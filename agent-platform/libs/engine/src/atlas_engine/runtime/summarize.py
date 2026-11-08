"""压缩用的摘要器：把一段对话压成结构化摘要（doc/context-management-design.html §9.1）。

上下文流水线的 Compactor 自动压缩时调它，手动压缩（server 的 /compact）也调它。用什么模型、
经哪个端点由调用方的模型构造器决定；这里只决定提示词、怎么渲染对话、怎么取摘要。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.messages import BaseMessage

from atlas_engine.contracts.messages import StoredLike
from atlas_engine.contracts.spec import ModelSpec
from atlas_engine.contracts.tool_names import ASK_USER_QUESTION
from atlas_engine.runtime.messages import history_messages
from atlas_engine.runtime.models import complete
from atlas_engine.runtime.translator import extract_text

__all__ = ["Summarizer", "render_conversation"]

logger = logging.getLogger(__name__)

#: 摘要结构（doc/context-management-design.html §9.1，在 §7.2 原四块上扩展到八段）。
#: ★ 第 3 段「已排除的方案及理由」最容易被丢，丢了 agent 会兜回去重试已否决的路径；
#:   第 6 段「全部用户消息」是用户意图的变化轨迹，摘要最容易把它抹平。
#: ★ 先在 <analysis> 里起草再写 <summary>，只保留后者（草稿让模型逐段检查覆盖面）。
_PROMPT = """把下面这段对话压缩成结构化摘要，供后续对话作为上下文使用。后续的你只能看到这份摘要
（加上最近几轮原文），所以凡是继续工作需要的信息都必须写进来。

先在 <analysis> 标签里按时间顺序梳理对话、检查有没有遗漏，然后在 <summary> 标签里输出摘要，
包含以下八段，缺一不可（某段确实没有内容就写「无」）：

1. 用户的原始目标与硬约束
2. 已确认的关键事实与数据 —— **必须带来源**（文件路径、命令输出、检索结果）
3. 已做出的决定，以及**已排除的方案和排除理由**
4. 涉及的文件、命令与关键片段（路径、函数名、关键代码或配置原文）
5. 遇到的错误与修复方式 —— **尤其是用户的纠正与反馈**
6. 全部用户消息，包括「用户（回答提问）」那几条（逐条列出，不含工具结果；可略去寒暄）
7. 未完成的事项
8. 当前正在做什么，以及下一步（必须与用户最近的明确请求一致，不要擅自开启新方向）

不要复述寒暄，不要写"这段对话讨论了……"这类开场白，直接给内容。

对话：
{conversation}"""


def _extract_summary(text: str) -> str:
    """只保留 <summary> 里的内容；模型没按格式写时退回全文（去掉 analysis 草稿）。"""
    import re

    if (m := re.search(r"<summary>(.*?)(?:</summary>|$)", text, re.S)) and m.group(1).strip():
        return m.group(1).strip()
    body = re.sub(r"</?summary>", "", text)
    rest = re.sub(r"<analysis>.*?(?:</analysis>|$)", "", body, flags=re.S).strip()
    if rest:
        return rest
    # ★ 有的模型（实测 deepseek-chat）写完草稿就结束、不出 <summary> 或只开了个头：
    #   草稿本身是按时间顺序整理的事实，退回用它，比整个压缩失败强
    return re.sub(r"</?analysis>", "", body).strip()


#: 单条消息进摘要时的截断长度。工具返回动辄几万字，全塞进去
#: 摘要调用本身就会超窗口。
_PER_MESSAGE_LIMIT = 2_000


def render_conversation(messages: Sequence[BaseMessage]) -> str:
    lines: list[str] = []
    for message in messages:
        text = extract_text(message.content)
        lines.append(f"[{_role_of(message)}] {text[:_PER_MESSAGE_LIMIT]}")
    return "\n".join(lines)


def _role_of(message: BaseMessage) -> str:
    """摘要里这一条算谁说的。

    ★ 用户对 ask_user_question 的回答在对话里是一条**工具结果**，但内容是用户说的话
      （doc/ask-user-question-design.html）。标成「工具返回」的话，摘要第 6 段「全部用户消息
      （不含工具结果）」恰好把它排除在外 —— 长对话压缩之后，用户确认过的需求就没了。
      按工具名（ToolMessage.name，随消息落库，见 runtime/messages.py 的 atlas_name）标成用户。
    """
    if message.type == "tool" and getattr(message, "name", None) == ASK_USER_QUESTION:
        return "用户（回答提问）"
    return {"human": "用户", "ai": "助手", "tool": "工具返回"}.get(message.type, message.type)


class Summarizer:
    """压缩用的摘要器。

    build_model: 按 ModelSpec 造一个聊天模型。端点、Key、遥测都在调用方 —— 自动压缩和手动压缩
    与主对话、标题生成共用同一个接缝，单测里换成假模型就不会真打网关（P5 踩过）。
    """

    def __init__(
        self,
        build_model: Callable[[ModelSpec], Any],
        *,
        model: str,
        provider: str = "anthropic",
    ) -> None:
        self._build_model = build_model
        self._model = model
        self._provider = provider

    @property
    def model(self) -> str:
        return self._model

    async def __call__(self, messages: Sequence[BaseMessage], *, focus: str = "") -> str:
        # 八段摘要 + 分析草稿，2048 不够
        chat = self._build_model(
            ModelSpec(
                model=self._model,
                provider=self._provider,  # type: ignore[arg-type]
                max_output_tokens=8192,
            )
        )
        prompt = _PROMPT.format(conversation=render_conversation(messages))
        if focus.strip():
            # 手动压缩时用户可以说明「重点保留什么」
            prompt += f"\n\n用户特别要求在摘要里保留：{focus.strip()[:2000]}"
        result = await complete(chat, prompt)
        # ★ 这段文本要写进 thread_context.summary，之后每一轮都带着它跑
        text = _extract_summary(result.text)
        if not text:
            logger.warning(
                "摘要为空：model=%s stop=%s raw_len=%d",
                self._model,
                result.stop_reason,
                len(result.text),
            )
            raise ValueError("摘要为空")
        return text

    async def summarize_history(
        self, rows: Sequence[StoredLike], *, summary: str | None = None, focus: str = ""
    ) -> str:
        """手动压缩：存储格式的历史（加上已有的摘要）→ 新摘要。"""
        return await self(history_messages(rows, summary=summary), focus=focus)
