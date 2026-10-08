"""技能使用量的按天汇总

Revision ID: 0014_skill_usage_daily
Revises: 0013_drop_foreign_keys
Create Date: 2026-10-05

doc/skill-mcp-backend-design.html §7.4。技能本身归配置服务（atlas-config），
这张表只记运行时观察到的使用：哪天、哪个 agent、哪个技能版本被加载了几次。
不建外键（0013 起的约定）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_skill_usage_daily"
down_revision: str | Sequence[str] | None = "0013_drop_foreign_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_usage_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("loads", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completed_runs", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint(
            "day", "slug", "version", "agent_id", name=op.f("pk_skill_usage_daily")
        ),
    )


def downgrade() -> None:
    op.drop_table("skill_usage_daily")
