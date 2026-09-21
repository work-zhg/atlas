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

from atlas_server.domain.spec import ModelSpec
from atlas_server.domain.translator import extract_text
from langchain_core.messages import BaseMessage, HumanMessage
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..db.models import Thread

logger = logging.getLogger(__name__)

#: §7.2：摘要必须包含四块，缺一不可。
#: 第 3 点「已排除的方案及理由」最容易被丢掉，丢了 agent 会兜回去重试
#: 已经否决过的路径 —— 这是压缩最贵的失败模式。
_PROMPT = """把下面这段对话压缩成结构化摘要，供后续对话作为上下文使用。

必须包含以下四块，缺一不可：

1. 用户的原始目标与硬约束
2. 已确认的关键事实与数据 —— **必须带来源**
3. 已做出的决定，以及**已排除的方案和排除理由**
4. 未完成的事项

不要复述寒暄，不要写"这段对话讨论了……"这类开场白，直接给内容。

对话：
{conversation}"""

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

    def __init__(self, settings: Settings, model_builder: Any) -> None:
        self._settings = settings
        self._build_model = model_builder

    async def __call__(self, messages: Sequence[BaseMessage]) -> str:
        chat = self._build_model(
            # 固定用 summarizer_model（haiku）：摘要不需要 opus 的推理能力，
            # 而这是一次输入很大的调用，用贵模型代价明显。
            ModelSpec(model=self._settings.summarizer_model, max_output_tokens=2048),
            base_url=str(self._settings.litellm_base_url),
            api_key=self._settings.litellm_key.get_secret_value(),
        )
        result = await chat.ainvoke(
            [HumanMessage(content=_PROMPT.format(conversation=_render(messages)))]
        )
        # ★ 同 title.py：str(content) 会把 content blocks 变成 Python repr，
        #   而这段文本是要写进 thread.summary 的 —— 之后每一轮都带着它跑。
        return extract_text(result.content)


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
                    )
                )
                await session.commit()
        except Exception:
            # 摘要没存住只是下轮要重压一次，不该让本轮失败
            logger.warning("摘要持久化失败 thread=%s", thread_id, exc_info=True)

    return persist
