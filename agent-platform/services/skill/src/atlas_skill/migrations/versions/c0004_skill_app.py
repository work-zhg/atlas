"""配置服务改为技能应用：持久指令搬到 Atlas，审计表改名

Revision ID: c0004_skill_app
Revises: c0003_drop_mcp
Create Date: 2026-10-29

instruction   持久指令是每轮装配进智能体上下文的东西，归 Atlas（atlas_agent 迁移 0027）。
              ★ 已有数据由 deploy/server/skill-cutover.sh 在本迁移之前搬到 Atlas 的库
config_audit  → skill_audit（库里只剩技能）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c0004_skill_app"
down_revision: str | Sequence[str] | None = "c0003_drop_mcp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.drop_table("instruction")
    op.rename_table("config_audit", "skill_audit")
    op.execute("ALTER INDEX ix_config_audit_target RENAME TO ix_skill_audit_target")


def downgrade() -> None:
    op.execute("ALTER INDEX ix_skill_audit_target RENAME TO ix_config_audit_target")
    op.rename_table("skill_audit", "config_audit")
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
