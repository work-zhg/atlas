"""持久指令：平台与用户两层（doc/context-management-design.html §10）

Revision ID: c0002_instruction
Revises: c0001_initial
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c0002_instruction"
down_revision: str | Sequence[str] | None = "c0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "instruction",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(255), nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.UniqueConstraint("scope", "owner_id", name="uq_instruction_scope_owner_id"),
        sa.CheckConstraint("scope IN ('platform','user')", name="ck_instruction_scope_enum"),
    )


def downgrade() -> None:
    op.drop_table("instruction")
