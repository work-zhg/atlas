"""会话的上下文状态与手动压缩（doc/context-management-design.html §16）。

    status   最近一次模型调用的上下文构成与用量（来自归档的 context.usage 事件）+ 摘要 / 熔断状态
    compact  手动压缩：把摘要之后的全部历史并入新摘要（可附「重点保留什么」），下一轮生效

★ 手动压缩只在会话空闲时做（有在跑的 run 时 409）：与 run 并发改摘要会互相覆盖。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from atlas_engine.kernel.middleware.compaction import SUMMARY_PREFIX
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import Agent, AgentVersion, RunEvent, Thread
from ..db.session import fetch_one
from ..domain.messages import from_storage
from ..domain.spec import resolve_summarizer_model
from ..errors import Conflict, NotFound
from ..providers.llm.model_platform import (
    admit_models,
    agent_key,
    key_endpoint,
    uses_model_platform,
)
from ..repositories.run import RunRepository
from ..repositories.thread_context import ThreadContextRepository
from .compaction import ThreadSummarizer

__all__ = ["ContextService"]


class ContextService:
    def __init__(self, session: AsyncSession, settings: Settings, model_builder: Any) -> None:
        self._s = session
        self._settings = settings
        self._build_model = model_builder

    async def _thread(self, thread_id: UUID) -> Thread:
        thread = await fetch_one(self._s, Thread, Thread.thread_uuid == thread_id)
        if thread is None:
            raise NotFound(f"会话 {thread_id} 不存在", thread_id=str(thread_id))
        return thread

    async def status(self, thread_id: UUID) -> dict[str, Any]:
        thread = await self._thread(thread_id)
        row = await self._s.scalar(
            select(RunEvent)
            .where(
                RunEvent.thread_uuid == thread.stream_thread_id,
                RunEvent.type == "context.usage",
                RunEvent.depth == 0,
            )
            .order_by(RunEvent.thread_seq.desc())
            .limit(1)
        )
        context = await ThreadContextRepository(self._s).get(thread_id)
        failures = context.failure_count if context is not None else 0
        summary = context.summary if context is not None else None
        upto = context.summary_upto if context is not None else None
        return {
            "usage": dict(row.data) if row is not None else None,
            "usage_at": row.ts.isoformat() if row is not None else None,
            "summary": {
                "present": bool(summary),
                "chars": len(summary or ""),
                "upto": upto.isoformat() if upto else None,
                "compact_count": context.compact_count if context is not None else 0,
            },
            "compaction_failures": failures,
            "compaction_disabled": failures >= 3,
        }

    async def compact(self, thread_id: UUID, *, focus: str = "") -> dict[str, Any]:
        thread = await self._thread(thread_id)
        runs = RunRepository(self._s)
        if await runs.active_run_of(thread_id) is not None:
            raise Conflict("会话里有正在执行的一轮，结束后再压缩", thread_id=str(thread_id))
        context = await ThreadContextRepository(self._s).get(thread_id)
        rows = await runs.history(
            thread_id, after=context.summary_upto if context is not None else None
        )
        if len(rows) < 2:
            raise Conflict("没有可压缩的新内容", thread_id=str(thread_id))
        history = from_storage(rows)
        prior = (
            [AIMessage(content=SUMMARY_PREFIX + context.summary)]
            if context is not None and context.summary
            else []
        )

        agent = await fetch_one(self._s, Agent, Agent.agent_uuid == thread.agent_uuid)
        model = ""
        if agent is not None:
            version = await self._s.scalar(
                select(AgentVersion)
                .where(AgentVersion.agent_uuid == agent.agent_uuid)
                .order_by(AgentVersion.version.desc())
                .limit(1)
            )
            spec = (version.spec if version is not None else {}) or {}
            comp = spec.get("compaction") or {}
            model = resolve_summarizer_model(
                comp.get("summarizer_model"), (spec.get("model") or {}).get("model") or ""
            )
        endpoint = None
        if uses_model_platform(self._settings) and agent is not None:
            # 经模型管理平台：智能体的客户端 Key，摘要模型要在它的可用模型里（同 Run 开始的检查）
            key = await agent_key(self._settings, agent.agent_uuid, agent.name)
            admit_models(key, [model or self._settings.summarizer_model])
            assert key.key is not None
            endpoint = key_endpoint(self._settings, key.key)
        summarizer = ThreadSummarizer(
            self._settings, self._build_model, model=model or None, endpoint=endpoint
        )
        summary = await summarizer([*prior, *history], focus=focus)

        await ThreadContextRepository(self._s).save_summary(
            thread_id, summary=summary, upto=rows[-1].created_at
        )
        await self._s.commit()
        return {"summary": summary, "messages_summarized": len(rows), "model": summarizer.model}
