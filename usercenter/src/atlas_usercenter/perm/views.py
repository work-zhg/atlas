"""权限相关的展示辅助（被用户 / 组织接口复用）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Position, User
from .effective import matched_grants

__all__ = ["positions_of"]


async def positions_of(session: AsyncSession, user: User) -> list[str]:
    """用户的岗位 = 其命中的全部岗位授权（可多个，可经部门获得）。"""
    ids = {g.position_uuid for g in await matched_grants(session, user) if g.kind == "position"}
    if not ids:
        return []
    return list(
        await session.scalars(
            select(Position.name).where(Position.uuid.in_(ids)).order_by(Position.sort)
        )
    )
