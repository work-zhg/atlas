"""给已有业务表加租户的 Alembic 迁移步骤（各服务的迁移脚本里调用）。

add_tenant(op, "agent", backfill=原平台租户)
    加可空列 → 回填 → 改非空 → 建索引。已有数据全部归 backfill 这个租户。
scope_unique(op, "agent", "uk_agent_slug", ["slug"])
    唯一约束改成带租户的复合唯一：名字只在租户内唯一。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import sqlalchemy as sa

__all__ = ["add_tenant", "drop_tenant", "scope_unique", "unscope_unique"]

COLUMN = "tenant_uuid"


def add_tenant(op: Any, table: str, *, backfill: UUID, index: bool = True) -> None:
    op.add_column(table, sa.Column(COLUMN, sa.Uuid(), nullable=True))
    t = sa.table(table, sa.column(COLUMN, sa.Uuid()))
    op.execute(t.update().values({COLUMN: backfill}))
    op.alter_column(table, COLUMN, existing_type=sa.Uuid(), nullable=False)
    if index:
        op.create_index(f"ix_{table}_{COLUMN}", table, [COLUMN])


def drop_tenant(op: Any, table: str, *, index: bool = True) -> None:
    """add_tenant 的反向（downgrade 用）。"""
    if index:
        op.drop_index(f"ix_{table}_{COLUMN}", table_name=table)
    op.drop_column(table, COLUMN)


def scope_unique(
    op: Any, table: str, old: str | None, columns: Sequence[str], *, name: str | None = None
) -> str:
    """把 old 唯一约束换成 (tenant_uuid, *columns) 的复合唯一，返回新约束名。

    old 为 None 表示原来没有。
    """
    if old:
        op.drop_constraint(old, table, type_="unique")
    new = name or f"uq_{table}_{COLUMN}_{'_'.join(columns)}"
    op.create_unique_constraint(new, table, [COLUMN, *columns])
    return new


def unscope_unique(op: Any, table: str, new: str, old: str, columns: Sequence[str]) -> None:
    """scope_unique 的反向（downgrade 用）。"""
    op.drop_constraint(new, table, type_="unique")
    op.create_unique_constraint(old, table, list(columns))
