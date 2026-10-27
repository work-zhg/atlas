"""记忆改用记忆服务（doc/memory-design.html）：会话的记忆状态、用户的记忆设置、补记结果

Revision ID: 0025_memory_service
Revises: 0024_drop_builtin_agent
Create Date: 2026-10-26

thread_memory        会话的记忆状态：实例登记、常驻快照、已知版本表、补记进度
memory_user_setting  用户级设置：暂停写入、管理操作计数（触发常驻刷新）
memory_backfill      会话后补记的结果，供「上次会话补记了 N 条」提示

★ Mem0 里的旧记忆不迁移（设计 Q5）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import (
    JSON_COL,
    JSON_COL_NULLABLE,
    TS_COL,
    UUID_COL,
    now_default,
)

revision: str = "0025_memory_service"
down_revision: str | Sequence[str] | None = "0024_drop_builtin_agent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "thread_memory",
        sa.Column("thread_id", UUID_COL, primary_key=True),
        sa.Column("registered_at", TS_COL, nullable=True),
        sa.Column("snapshot", JSON_COL_NULLABLE, nullable=True),
        sa.Column("known", JSON_COL_NULLABLE, nullable=True),
        sa.Column("backfill_upto", TS_COL, nullable=True),
        sa.Column("backfill_seen", TS_COL, nullable=True),
        sa.Column("backfill_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("backfill_error", sa.Text(), nullable=True),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
    )
    op.create_table(
        "memory_user_setting",
        sa.Column("user_id", UUID_COL, primary_key=True),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
    )
    op.create_table(
        "memory_backfill",
        sa.Column("id", UUID_COL, primary_key=True),
        sa.Column("thread_id", UUID_COL, nullable=False),
        sa.Column("user_id", UUID_COL, nullable=False),
        sa.Column("written", JSON_COL, nullable=False),
        sa.Column("split", JSON_COL, nullable=False),
        sa.Column("created_at", TS_COL, nullable=False, server_default=now_default()),
        sa.Column("seen_at", TS_COL, nullable=True),
    )
    op.create_index("ix_memory_backfill_user", "memory_backfill", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_memory_backfill_user", table_name="memory_backfill")
    op.drop_table("memory_backfill")
    op.drop_table("memory_user_setting")
    op.drop_table("thread_memory")
