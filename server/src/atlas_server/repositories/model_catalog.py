from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ModelCatalog


class ModelCatalogRepository:
    """唯一接触 model_catalog 表的地方（文档 §2.1 分层规则）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(self) -> list[ModelCatalog]:
        stmt = select(ModelCatalog).order_by(ModelCatalog.model)
        result = await self._session.execute(stmt)
        return list(result.scalars())

    async def context_window(self, model: str) -> int | None:
        """§7.1 触发阈值的基准。实测落库，不硬编码（§3 D2）。"""
        stmt = select(ModelCatalog.context_window).where(ModelCatalog.model == model)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def sync_availability(self, available_models: set[str]) -> int:
        """按网关巡检结果刷新 is_available。返回状态发生变化的行数。"""
        rows = await self.list_all()
        changed = 0
        for row in rows:
            desired = row.model in available_models
            if row.is_available != desired:
                row.is_available = desired
                changed += 1
        return changed
