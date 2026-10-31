"""持久指令从配置服务搬到 Atlas（配置服务改为技能应用，只管技能）

Revision ID: 0027_instruction
Revises: 0026_service_model
Create Date: 2026-10-29

instruction  平台 / 用户两层，一层一行（doc/context-management-design.html §10）。
             ★ 已有数据由 deploy/server/skill-cutover.sh 从原配置库的 instruction 表搬过来
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import TS_COL, UUID_COL, now_default

revision: str = "0027_instruction"
down_revision: str | Sequence[str] | None = "0026_service_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instruction",
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("owner_id", sa.String(64), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("updated_by", UUID_COL, nullable=True),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
        sa.CheckConstraint("scope IN ('platform','user')", name="ck_instruction_scope_enum"),
    )


def downgrade() -> None:
    op.drop_table("instruction")
