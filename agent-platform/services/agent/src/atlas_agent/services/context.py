"""会话的上下文状态与手动压缩（doc/context-management-design.html §16）。

    status   最近一次模型调用的上下文构成与用量（来自归档的 context.usage 事件）+ 摘要 / 熔断状态
    compact  手动压缩：把摘要之后的全部历史并入新摘要（可附「重点保留什么」），下一轮生效

★ 手动压缩只在会话空闲时做（有在跑的 run 时 409）：与 run 并发改 thread.summary 会互相覆盖。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from atlas_engine.kernel.middleware.compaction import SUMMARY_PREFIX
from langchain_core.messages import AIMessage
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import Agent, AgentVersion, RunEvent, Thread
from ..domain.messages import from_storage
from ..domain.spec import resolve_summarizer_model
from ..errors import Conflict, NotFound
from ..repositories.run import RunRepository
from .compaction import ThreadSummarizer

__all__ = ["ContextService"]


class ContextService:
    def __init__(self, session: AsyncSession, settings: Settings, model_builder: Any) -> None:
        self._s = session
        self._settings = settings
        self._build_model = model_builder

    async def _thread(self, thread_id: UUID) -> Thread:
        thread = await self._s.get(Thread, thread_id)
        if thread is None:
            raise NotFound(f"会话 {thread_id} 不存在", thread_id=str(thread_id))
        return thread

    async def status(self, thread_id: UUID) -> dict[str, Any]:
        thread = await self._thread(thread_id)
        row = await self._s.scalar(
            select(RunEvent)
            .where(
                RunEvent.thread_id == thread.stream_thread_id,
                RunEvent.type == "context.usage",
                RunEvent.depth == 0,
            )
            .order_by(RunEvent.thread_seq.desc())
            .limit(1)
        )
        state = thread.context_state or {}
        failures = int(state.get("compaction_failures") or 0)
        return {
            "usage": dict(row.data) if row is not None else None,
            "usage_at": row.ts.isoformat() if row is not None else None,
            "summary": {
                "present": bool(thread.summary),
                "chars": len(thread.summary or ""),
                "upto": thread.summary_upto.isoformat() if thread.summary_upto else None,
                "compact_count": thread.compact_count,
            },
            "compaction_failures": failures,
            "compaction_disabled": failures >= 3,
        }

    async def compact(self, thread_id: UUID, *, focus: str = "") -> dict[str, Any]:
        thread = await self._thread(thread_id)
        runs = RunRepository(self._s)
        if await runs.active_run_of(thread_id) is not None:
            raise Conflict("会话里有正在执行的一轮，结束后再压缩", thread_id=str(thread_id))
        rows = await runs.history(thread_id, after=thread.summary_upto)
        if len(rows) < 2:
            raise Conflict("没有可压缩的新内容", thread_id=str(thread_id))
        history = from_storage(rows)
        prior = [AIMessage(content=SUMMARY_PREFIX + thread.summary)] if thread.summary else []

        agent = await self._s.get(Agent, thread.agent_id)
        model = ""
        if agent is not None:
            version = await self._s.scalar(
                select(AgentVersion)
                .where(AgentVersion.agent_id == agent.id)
                .order_by(AgentVersion.version.desc())
                .limit(1)
            )
            spec = (version.spec if version is not None else {}) or {}
            comp = spec.get("compaction") or {}
            model = resolve_summarizer_model(
                comp.get("summarizer_model"), (spec.get("model") or {}).get("model") or ""
            )
        summarizer = ThreadSummarizer(self._settings, self._build_model, model=model or None)
        summary = await summarizer([*prior, *history], focus=focus)

        await self._s.execute(
            update(Thread)
            .where(Thread.id == thread_id)
            .values(
                summary=summary,
                summary_upto=rows[-1].created_at,
                compact_count=Thread.compact_count + 1,
                context_state={"compaction_failures": 0},
            )
        )
        await self._s.commit()
        return {"summary": summary, "messages_summarized": len(rows), "model": summarizer.model}
