"""调用记录的查询：管理员看本租户的全部，个人只看自己 Key 的。只有数字，没有请求和回答的内容。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import GwModelCall

__all__ = ["CallFilter", "CallLog"]


@dataclass(frozen=True)
class CallFilter:
    #: None = 不限（管理员）；个人传自己的 Key
    key_uuids: Sequence[UUID] | None = None
    model: str | None = None
    status: str | None = None
    since: datetime | None = None
    until: datetime | None = None


class CallLog:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _where(self, stmt: Any, f: CallFilter) -> Any:
        if f.key_uuids is not None:
            stmt = stmt.where(GwModelCall.key_uuid.in_(list(f.key_uuids)))
        if f.model:
            stmt = stmt.where(GwModelCall.model == f.model)
        if f.status:
            stmt = stmt.where(GwModelCall.status == f.status)
        if f.since:
            stmt = stmt.where(GwModelCall.started_at >= f.since)
        if f.until:
            stmt = stmt.where(GwModelCall.started_at < f.until)
        return stmt

    async def page(
        self, f: CallFilter, *, limit: int = 50, before: datetime | None = None
    ) -> list[GwModelCall]:
        stmt = (
            self._where(select(GwModelCall), f)
            .order_by(GwModelCall.started_at.desc())
            .limit(min(limit, 200))
        )
        if before:
            stmt = stmt.where(GwModelCall.started_at < before)
        return list(await self.session.scalars(stmt))

    async def summary(self, f: CallFilter, *, by: str) -> list[dict[str, Any]]:
        """按模型、Key 或天汇总：次数、失败次数、四项用量、费用。"""
        column = {
            "model": GwModelCall.model,
            "key": GwModelCall.key_uuid,
            "day": cast(GwModelCall.started_at, Date),
        }[by]
        stmt = (
            self._where(
                select(
                    column.label("group"),
                    func.count().label("calls"),
                    func.sum(case((GwModelCall.status != "ok", 1), else_=0)).label("errors"),
                    func.sum(GwModelCall.input_tokens).label("input"),
                    func.sum(GwModelCall.cache_write_tokens).label("cache_write"),
                    func.sum(GwModelCall.cache_hit_tokens).label("cache_hit"),
                    func.sum(GwModelCall.output_tokens).label("output"),
                    func.sum(GwModelCall.cost).label("cost"),
                ),
                f,
            )
            .group_by(column)
            .order_by(func.sum(GwModelCall.cost).desc())
        )
        rows = await self.session.execute(stmt)
        return [
            {
                "group": str(r.group),
                "calls": int(r.calls),
                "errors": int(r.errors or 0),
                "input": int(r.input or 0),
                "cache_write": int(r.cache_write or 0),
                "cache_hit": int(r.cache_hit or 0),
                "output": int(r.output or 0),
                "cost": r.cost or Decimal(0),
            }
            for r in rows
        ]

    async def last_used(
        self, key_uuids: Sequence[UUID], *, since: datetime
    ) -> dict[UUID, datetime]:
        if not key_uuids:
            return {}
        rows = await self.session.execute(
            select(GwModelCall.key_uuid, func.max(GwModelCall.started_at))
            .where(GwModelCall.key_uuid.in_(list(key_uuids)), GwModelCall.started_at >= since)
            .group_by(GwModelCall.key_uuid)
        )
        return {k: t for k, t in rows if k is not None}

    async def spent(self, key_uuids: Sequence[UUID], *, since: datetime) -> dict[UUID, Decimal]:
        if not key_uuids:
            return {}
        rows = await self.session.execute(
            select(GwModelCall.key_uuid, func.sum(GwModelCall.cost))
            .where(GwModelCall.key_uuid.in_(list(key_uuids)), GwModelCall.started_at >= since)
            .group_by(GwModelCall.key_uuid)
        )
        return {k: v or Decimal(0) for k, v in rows if k is not None}
