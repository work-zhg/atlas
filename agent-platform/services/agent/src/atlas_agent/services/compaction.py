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
from typing import Any
from uuid import UUID

from atlas_engine.contracts.spec import ModelSpec
from atlas_engine.runtime.summarize import Summarizer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..providers.llm.model_platform import ModelEndpoint
from ..repositories.run import RunRepository
from ..repositories.thread_context import ThreadContextRepository

logger = logging.getLogger(__name__)


def thread_summarizer(
    model_builder: Any,
    model: str,
    *,
    endpoint: ModelEndpoint,
    provider: str = "anthropic",
) -> Summarizer:
    """压缩用的摘要器：模型经 model_builder 注入，与执行器、标题生成共用同一个接缝。

    model：智能体配置的 summarizer_model，没配就是智能体自己的模型（resolve_summarizer_model）。
    ★ 不写死 haiku：网关上未必有它，摘要一失败就只能降级丢弃早期对话。
    endpoint：模型网关 + 智能体的客户端 Key（摘要模型已检查过在它的可用模型里）。
    """

    def build(spec: ModelSpec) -> Any:
        return model_builder(
            spec, base_url=endpoint.base_url, api_key=endpoint.api_key, headers=endpoint.headers
        )

    return Summarizer(build, model=model, provider=provider)


def make_persist_hook(sessionmaker: async_sessionmaker[AsyncSession], thread_id: UUID):
    """返回 on_compacted 回调：把摘要和覆盖边界写进会话的上下文状态（thread_context）。

    覆盖边界 = 保留区第一条消息（payload 的 kept_from，库里那一行的身份）之前最后一条消息的时间。
    ★ 不能用「本次 run 起始时历史的最后一条」：自动压缩留下最近几个回合不进摘要，边界落在历史
      末尾的话，下一轮按边界切历史时这几个回合既不在摘要里、也不在原文里，就丢了。
    ★ 找不到边界就不存（kept_from 不是这个会话里的消息、或者保留区前面没有消息）：存一个猜出来的
      边界，不是丢回合就是重复；不存只是下一轮再压一次。
    """

    async def persist(payload: dict[str, Any]) -> None:
        kept_from = _message_id(payload.get("kept_from"))
        if kept_from is None:
            logger.warning("压缩结果没有保留区的起点，不存摘要 thread=%s", thread_id)
            return
        try:
            async with sessionmaker() as session:
                upto = await RunRepository(session).last_before(thread_id, kept_from)
                if upto is None:
                    logger.warning(
                        "保留区起点 %s 之前没有消息，不存摘要 thread=%s", kept_from, thread_id
                    )
                    return
                # 成功一次就清零熔断计数
                await ThreadContextRepository(session).save_summary(
                    thread_id, summary=str(payload.get("summary", "")), upto=upto
                )
                await session.commit()
        except Exception:
            # 摘要没存住只是下轮要重压一次，不该让本轮失败
            logger.warning("摘要持久化失败 thread=%s", thread_id, exc_info=True)

    return persist


def _message_id(value: Any) -> UUID | None:
    """kept_from → 库里消息的身份。

    框架自己分配的 id 也是 UUID：它们在库里查不到行，边界就是 None（见 last_before）。
    """
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


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
