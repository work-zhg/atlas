"""智能体草稿与发布、调试预览（控制台改版 · 智能体编辑「编排 + 调试预览」）

Revision ID: 0042_agent_draft
Revises: 0041_message_feedback
Create Date: 2026-11-11

agent.draft_spec       编辑器里的修改自动存成草稿，不产生版本；「发布」时才写版本
agent.draft_updated_at 草稿最近一次保存的时间
agent_version.kind     release = 发布出的版本；preview = 调试预览时按草稿拍的快照（不进版本历史、
                       版本号取负数，不占发布版本的编号）
agent_version.note     发布时写的「这次改了什么」
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import JSON_COL_NULLABLE, TS_COL

revision: str = "0042_agent_draft"
down_revision: str | Sequence[str] | None = "0041_message_feedback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent", sa.Column("draft_spec", JSON_COL_NULLABLE, nullable=True))
    op.add_column("agent", sa.Column("draft_updated_at", TS_COL, nullable=True))
    op.add_column(
        "agent_version",
        sa.Column("kind", sa.String(16), nullable=False, server_default="release"),
    )
    op.add_column(
        "agent_version", sa.Column("note", sa.Text(), nullable=False, server_default="")
    )
    op.create_check_constraint(
        "ck_agent_version_kind", "agent_version", "kind IN ('release','preview')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_version_kind", "agent_version", type_="check")
    op.drop_column("agent_version", "note")
    op.drop_column("agent_version", "kind")
    op.drop_column("agent", "draft_updated_at")
    op.drop_column("agent", "draft_spec")
