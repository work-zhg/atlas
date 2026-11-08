"""上下文压缩的服务端接线（文档 §7）。

engine 负责「何时压、从哪切、怎么摘要」（kernel/context 与 runtime/summarize.py）；这里负责
「用什么模型摘要」与「摘要存哪」。

★ §7.4 的持久化：本项目没有 checkpointer，历史每轮从 message 表重建。
  照搬「改写 checkpoint」会变成每轮重压一次 —— 摘要 token 重复付、
  prompt cache 每轮击穿，正是 §7.3 坑 2 警告的抖动。
  等价做法是把摘要与覆盖边界存在 thread_context 上。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from atlas_engine.contracts.spec import ModelSpec
from atlas_engine.runtime.summarize import Summarizer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..providers.llm.model_platform import ModelEndpoint, direct_endpoint
from ..repositories.thread_context import ThreadContextRepository

logger = logging.getLogger(__name__)


def thread_summarizer(
    settings: Settings,
    model_builder: Any,
    model: str | None = None,
    *,
    endpoint: ModelEndpoint | None = None,
    provider: str = "anthropic",
) -> Summarizer:
    """压缩用的摘要器：模型经 model_builder 注入，与执行器、标题生成共用同一个接缝。

    model：智能体配置的 summarizer_model，空则用部署配置的 summarizer_model。
    ★ 不写死 haiku：网关上未必有它，摘要一失败就只能降级丢弃早期对话。
    endpoint：经模型管理平台时是 LiteLLM + 智能体的客户端 Key（Run 开始时已检查过）；
    空 = 没接平台，直连 litellm。
    """
    target = endpoint or direct_endpoint(settings)

    def build(spec: ModelSpec) -> Any:
        return model_builder(
            spec, base_url=target.base_url, api_key=target.api_key, headers=target.headers
        )

    return Summarizer(build, model=model or settings.summarizer_model, provider=provider)


def make_persist_hook(
    sessionmaker: async_sessionmaker[AsyncSession],
    thread_id: UUID,
    upto: datetime | None,
):
    """返回 on_compacted 回调：把摘要写进会话的上下文状态（thread_context）。

    `upto` 是本次 run 起始时历史里最后一条消息的时间 —— 摘要覆盖到那里。
    ★ 它可以为 None：会话的第一轮没有历史，也就没有覆盖边界。
      那种情况下不该写 summary_upto，否则下一轮会从一个错误的边界切历史。
    """

    async def persist(payload: dict[str, Any]) -> None:
        if upto is None:
            return
        try:
            async with sessionmaker() as session:
                # 成功一次就清零熔断计数
                await ThreadContextRepository(session).save_summary(
                    thread_id, summary=str(payload.get("summary", "")), upto=upto
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
                await ThreadContextRepository(session).record_failure(thread_id, failures)
                await session.commit()
        except Exception:
            logger.warning("记录压缩失败次数失败 thread=%s", thread_id, exc_info=True)

    return record
