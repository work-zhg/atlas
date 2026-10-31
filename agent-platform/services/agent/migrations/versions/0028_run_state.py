"""run 的图状态里 message 表装不下的部分（todos 等），续跑与接管时交回图

Revision ID: 0028_run_state
Revises: 0027_instruction
Create Date: 2026-10-30

run_state  一个 state 键一行，主键 (run_id, key)。作用域是 run：新的 run 读不到任何行。
           ★ 与其它表一样不建外键，删会话时由 ThreadRepository.delete 一并删除
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import JSON_COL, TS_COL, UUID_COL, now_default

revision: str = "0028_run_state"
down_revision: str | Sequence[str] | None = "0027_instruction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_state",
        sa.Column("run_id", UUID_COL, primary_key=True),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", JSON_COL, nullable=False),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
    )


def downgrade() -> None:
    op.drop_table("run_state")
