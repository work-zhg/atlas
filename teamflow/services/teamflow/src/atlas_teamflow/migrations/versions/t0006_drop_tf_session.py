"""drop_tf_session：登录统一到用户中心，网关把身份写进请求头（内网身份传递 §4、§10）

TeamFlow 不再有自己的登录会话：授权快照改为进程内缓存（identity.py）。上线时所有人从用户中心
重新登录一次。

Revision ID: t0006
Revises: t0005
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "t0006"
down_revision: str | Sequence[str] | None = "t0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(op.f("ix_tf_session_user_uuid"), table_name="tf_session")
    op.drop_table("tf_session")


def downgrade() -> None:
    op.create_table(
        "tf_session",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_uuid", sa.Uuid(), nullable=False),
        sa.Column(
            "snapshot",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("uuid", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_uuid"],
            ["tf_user.uuid"],
            name=op.f("fk_tf_session_user_uuid"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tf_session")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_tf_session_token_hash")),
        sa.UniqueConstraint("uuid", name=op.f("uq_tf_session_uuid")),
    )
    op.create_index(op.f("ix_tf_session_user_uuid"), "tf_session", ["user_uuid"], unique=False)
