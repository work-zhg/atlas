"""技能使用量（技能 / MCP 设计 §7.4）。"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AgentVersion, Run, RunEvent, SkillUsageDaily

logger = logging.getLogger(__name__)

__all__ = ["SkillUsageRepository"]

_LOADED = "skill.loaded"


class SkillUsageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_run(self, run_id: UUID, *, succeeded: bool, day: date) -> None:
        """run 进入终态时调用：从它的事件归档汇总这一轮加载过哪些技能。

        ★ 包在 savepoint 里且吞掉异常：统计失败绝不能让 run 的收尾（状态、
          消息、事件归档）一起回滚。
        """
        try:
            async with self._session.begin_nested():
                await self._record(run_id, succeeded=succeeded, day=day)
        except Exception:
            logger.warning("技能使用量记录失败 run=%s（不影响 run 本身）", run_id, exc_info=True)

    async def _record(self, run_id: UUID, *, succeeded: bool, day: date) -> None:
        rows = (
            await self._session.execute(
                select(RunEvent.data).where(RunEvent.run_id == run_id, RunEvent.type == _LOADED)
            )
        ).scalars()
        loaded = {
            (str(d.get("slug")), int(d.get("version") or 0))
            for d in rows
            if isinstance(d, dict) and d.get("slug")
        }
        if not loaded:
            return
        agent_id = await self._session.scalar(
            select(AgentVersion.agent_id)
            .join(Run, Run.agent_version_id == AgentVersion.id)
            .where(Run.id == run_id)
        )
        if agent_id is None:
            return
        for slug, version in sorted(loaded):
            await self._upsert(day, slug, version, agent_id, completed=1 if succeeded else 0)

    async def _upsert(
        self, day: date, slug: str, version: int, agent_id: UUID, *, completed: int
    ) -> None:
        values = {
            "day": day,
            "slug": slug,
            "version": version,
            "agent_id": agent_id,
            "loads": 1,
            "completed_runs": completed,
        }
        table = SkillUsageDaily.__table__
        dialect = self._session.bind.dialect.name if self._session.bind else "postgresql"
        if dialect in ("mysql", "mariadb"):
            from sqlalchemy.dialects.mysql import insert as mysql_insert

            stmt: Any = mysql_insert(table).values(**values)
            stmt = stmt.on_duplicate_key_update(
                loads=table.c.loads + 1, completed_runs=table.c.completed_runs + completed
            )
        else:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(table).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["day", "slug", "version", "agent_id"],
                set_={
                    "loads": table.c.loads + 1,
                    "completed_runs": table.c.completed_runs + completed,
                },
            )
        await self._session.execute(stmt)

    async def summary(self, *, days: int, today: date) -> list[dict[str, Any]]:
        """近 N 天按 (slug, version, agent) 汇总。"""
        since = today - timedelta(days=days - 1)
        rows = (
            await self._session.execute(
                select(
                    SkillUsageDaily.slug,
                    SkillUsageDaily.version,
                    SkillUsageDaily.agent_id,
                    func.sum(SkillUsageDaily.loads).label("loads"),
                    func.sum(SkillUsageDaily.completed_runs).label("completed_runs"),
                )
                .where(SkillUsageDaily.day >= since)
                .group_by(SkillUsageDaily.slug, SkillUsageDaily.version, SkillUsageDaily.agent_id)
                .order_by(SkillUsageDaily.slug, SkillUsageDaily.version)
            )
        ).all()
        return [
            {
                "slug": r.slug,
                "version": r.version,
                "agent_id": r.agent_id,
                "loads": int(r.loads or 0),
                "completed_runs": int(r.completed_runs or 0),
            }
            for r in rows
        ]
