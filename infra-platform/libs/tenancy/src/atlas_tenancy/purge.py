"""删除一个租户在本服务数据库里的全部数据：按宿主给的顺序（子表在前），分批。

★ 只能在 OPS 系统范围里调用：由运营接口库在删完外部资源（文件、向量、Pod）之后调。
  数据库里的行是找到外部资源的线索，所以数据库最后删。
★ 本仓不建外键，顺序没法从元数据推出来，由宿主显式给出：子表先删，中途停下也不会留下
  指向已删父行的孤儿。漏了哪张租户表，构造时就报错。
★ 这里用 Core 的 DELETE（不受自动过滤），条件显式写租户 —— 这是本库允许的少数几处之一。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from .context import SystemReason, TenantScope, TenantViolation
from .model import TenantOwned

__all__ = ["PurgeReport", "TenantDataPurge"]


@dataclass
class PurgeReport:
    """一批删除的结果：各表删了多少行、还有没有剩。只有数字，不含任何租户内容。"""

    deleted: dict[str, int] = field(default_factory=dict)
    remaining: bool = False

    def merge(self, other: PurgeReport) -> PurgeReport:
        for k, v in other.deleted.items():
            self.deleted[k] = self.deleted.get(k, 0) + v
        self.remaining = other.remaining
        return self


class TenantDataPurge:
    def __init__(self, order: Sequence[type[TenantOwned]], *, base: type | None = None) -> None:
        """order：全部租户表的模型类，子表在前。给了 base 就核对 order 没有漏掉任何租户表。"""
        tables = [sa.inspect(m).local_table for m in order]
        if len({t.name for t in tables}) != len(tables):
            raise ValueError("删除顺序里有重复的表")
        if base is not None:
            owned = {
                m.local_table.name
                for m in base.registry.mappers  # type: ignore[attr-defined]
                if issubclass(m.class_, TenantOwned)
            }
            missing = owned - {t.name for t in tables}
            if missing:
                raise ValueError(f"删除顺序漏了租户表：{sorted(missing)}")
        self._tables = tables

    async def batch(
        self, session: AsyncSession, tenant_uuid: UUID, limit: int = 5000
    ) -> PurgeReport:
        """按顺序删，最多删 limit 行；调用方提交事务。删不完 remaining = True，再调一次接着删。"""
        if TenantScope.system_reason() is not SystemReason.OPS:
            raise TenantViolation(TenantViolation.MISSING, "删除租户数据只能在 OPS 系统范围里做")
        report = PurgeReport()
        budget = limit
        for table in self._tables:
            if budget <= 0:
                break
            # ★ 按主键第一列分批：复合主键（分区表的主键要带分区键，如 gw_model_call）的第一列
            #   不保证跨租户唯一，外层也带上租户条件，删不到别的租户
            pk = next(iter(table.primary_key.columns))
            mine = table.c.tenant_uuid == tenant_uuid
            ids = sa.select(pk).where(mine).limit(budget)
            result = await session.execute(
                sa.delete(table).where(mine, pk.in_(ids.scalar_subquery()))
            )
            n = result.rowcount or 0
            if n:
                report.deleted[table.name] = n
                budget -= n
        if budget <= 0:
            report.remaining = await self._any_left(session, tenant_uuid)
        return report

    async def _any_left(self, session: AsyncSession, tenant_uuid: UUID) -> bool:
        for table in self._tables:
            hit = await session.scalar(
                sa.select(sa.literal(1)).where(table.c.tenant_uuid == tenant_uuid).limit(1)
            )
            if hit:
                return True
        return False
