"""回答反馈：有帮助 / 没帮助（控制台改版 · 对话页每条回答下的反馈）

Revision ID: 0041_message_feedback
Revises: 0040_sandbox_execution
Create Date: 2026-11-11

一个人对一条回答只有一条反馈，再点就是改：(message_uuid, user_uuid) 唯一。
没帮助时可以带原因（固定选项）和补充说明。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import JSON_COL, TS_COL, UUID_COL, now_default

revision: str = "0041_message_feedback"
down_revision: str | Sequence[str] | None = "0040_sandbox_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_feedback",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("feedback_uuid", UUID_COL, nullable=False),
        sa.Column("message_uuid", UUID_COL, nullable=False),
        sa.Column("thread_uuid", UUID_COL, nullable=False),
        sa.Column("run_uuid", UUID_COL, nullable=True),
        sa.Column("user_uuid", UUID_COL, nullable=False),
        sa.Column("rating", sa.String(8), nullable=False),
        sa.Column("reasons", JSON_COL, nullable=False),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", TS_COL, nullable=False, server_default=now_default()),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
        sa.PrimaryKeyConstraint("id", name="pk_message_feedback"),
        sa.UniqueConstraint("feedback_uuid", name="uk_message_feedback_uuid"),
        sa.UniqueConstraint("message_uuid", "user_uuid", name="uk_message_feedback_user"),
        sa.CheckConstraint("rating IN ('up','down')", name="ck_message_feedback_rating"),
    )
    op.create_index("ix_message_feedback_thread", "message_feedback", ["thread_uuid"])


def downgrade() -> None:
    op.drop_index("ix_message_feedback_thread", table_name="message_feedback")
    op.drop_table("message_feedback")
