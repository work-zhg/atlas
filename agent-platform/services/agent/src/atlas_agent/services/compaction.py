"""上下文压缩的服务端接线（文档 §7）。

engine 负责「何时压、从哪切」；这里负责「用什么模型摘要」与「摘要存哪」。

★ §7.4 的持久化：本项目没有 checkpointer，历史每轮从 message 表重建。
  照搬「改写 checkpoint」会变成每轮重压一次 —— 摘要 token 重复付、
  prompt cache 每轮击穿，正是 §7.3 坑 2 警告的抖动。
  等价做法是把摘要与覆盖边界存在 thread 上（迁移 0005）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from langchain_core.messages import BaseMessage, HumanMessage
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas_agent.domain.spec import ModelSpec
from atlas_agent.domain.translator import extract_text

from ..config import Settings
from ..db.models import Thread

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
6. 全部用户消息（逐条列出，不含工具结果；可略去寒暄）
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


def _render(messages: Sequence[BaseMessage]) -> str:
    lines: list[str] = []
    for message in messages:
        text = extract_text(message.content)
        role = {"human": "用户", "ai": "助手", "tool": "工具返回"}.get(message.type, message.type)
        lines.append(f"[{role}] {text[:_PER_MESSAGE_LIMIT]}")
    return "\n".join(lines)


class ThreadSummarizer:
    """注入给 engine 的摘要器。

    模型经 model_builder 注入，与执行器、标题生成共用同一个接缝 ——
    自己调 build_chat_model 会绕过它，单测里就变成真打网关（P5 踩过）。
    """

    def __init__(self, settings: Settings, model_builder: Any, model: str | None = None) -> None:
        self._settings = settings
        self._build_model = model_builder
        #: 摘要模型：智能体配置的 summarizer_model，空则用智能体自己的模型（assembly 决定）。
        #: ★ 不再写死 haiku：网关上未必有它，摘要一失败就只能降级丢弃早期对话
        self._model = model or settings.summarizer_model

    @property
    def model(self) -> str:
        return self._model

    async def __call__(self, messages: Sequence[BaseMessage], *, focus: str = "") -> str:
        chat = self._build_model(
            # 八段摘要 + 分析草稿，2048 不够
            ModelSpec(model=self._model, max_output_tokens=8192),
            base_url=str(self._settings.litellm_base_url),
            api_key=self._settings.litellm_key.get_secret_value(),
        )
        prompt = _PROMPT.format(conversation=_render(messages))
        if focus.strip():
            # 手动压缩时用户可以说明「重点保留什么」
            prompt += f"\n\n用户特别要求在摘要里保留：{focus.strip()[:2000]}"
        result = await chat.ainvoke([HumanMessage(content=prompt)])
        # ★ 同 title.py：str(content) 会把 content blocks 变成 Python repr，
        #   而这段文本是要写进 thread.summary 的 —— 之后每一轮都带着它跑。
        text = _extract_summary(extract_text(result.content))
        if not text:
            meta = getattr(result, "response_metadata", None) or {}
            logger.warning(
                "摘要为空：model=%s stop=%s raw_len=%d",
                self._model,
                meta.get("stop_reason") or meta.get("finish_reason"),
                len(extract_text(result.content)),
            )
            raise ValueError("摘要为空")
        return text


def make_persist_hook(
    sessionmaker: async_sessionmaker[AsyncSession],
    thread_id: UUID,
    upto: datetime | None,
):
    """返回 on_compacted 回调：把摘要写回 thread。

    `upto` 是本次 run 起始时历史里最后一条消息的时间 —— 摘要覆盖到那里。
    ★ 它可以为 None：会话的第一轮没有历史，也就没有覆盖边界。
      那种情况下不该写 summary_upto，否则下一轮会从一个错误的边界切历史。
    """

    async def persist(payload: dict[str, Any]) -> None:
        if upto is None:
            return
        try:
            async with sessionmaker() as session:
                await session.execute(
                    update(Thread)
                    .where(Thread.id == thread_id)
                    .values(
                        summary=payload.get("summary", ""),
                        summary_upto=upto,
                        compact_count=Thread.compact_count + 1,
                        # 成功一次就清零熔断计数
                        context_state={"compaction_failures": 0},
                    )
                )
                await session.commit()
        except Exception:
            # 摘要没存住只是下轮要重压一次，不该让本轮失败
            logger.warning("摘要持久化失败 thread=%s", thread_id, exc_info=True)

    return persist


def make_failure_hook(sessionmaker: async_sessionmaker[AsyncSession], thread_id: UUID):
    """返回 on_failed 回调：记下自动压缩连续失败次数（熔断用，上下文管理设计 §7）。"""

    async def record(failures: int) -> None:
        try:
            async with sessionmaker() as session:
                await session.execute(
                    update(Thread)
                    .where(Thread.id == thread_id)
                    .values(context_state={"compaction_failures": failures})
                )
                await session.commit()
        except Exception:
            logger.warning("记录压缩失败次数失败 thread=%s", thread_id, exc_info=True)

    return record
