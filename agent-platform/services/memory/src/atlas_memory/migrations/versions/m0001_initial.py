"""记忆服务的初始表（doc/memory-design.html §3）。

Revision ID: m0001
Revises:
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "m0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)


def _row() -> list[sa.Column]:
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("uuid", sa.Uuid(), nullable=False, unique=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "mem_scope",
        *_row(),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("key_fields", postgresql.JSONB(), nullable=False),
        sa.Column("check_permission", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("data_code", sa.String(64), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
    )
    op.create_table(
        "mem_instance",
        *_row(),
        sa.Column("scope_uuid", sa.Uuid(), nullable=False),
        sa.Column("key", postgresql.JSONB(), nullable=False),
        sa.Column("key_text", sa.String(512), nullable=False),
        sa.Column("version_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("entry_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("uc_registered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("owner_uid", sa.String(64), nullable=True),
        sa.UniqueConstraint("scope_uuid", "key_text", name="uq_mem_instance_scope_uuid_key_text"),
    )
    op.create_index("ix_mem_instance_key", "mem_instance", ["key"], postgresql_using="gin")
    op.create_table(
        "mem_topic",
        *_row(),
        sa.Column("instance_uuid", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("attrs", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("entry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("split_into", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint("status IN ('active','split','deleted')", name="ck_mem_topic_status"),
    )
    op.create_index(
        "ux_mem_topic_code",
        "mem_topic",
        ["instance_uuid", "code"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "mem_entry",
        *_row(),
        sa.Column("instance_uuid", sa.Uuid(), nullable=False),
        sa.Column("topic_uuid", sa.Uuid(), nullable=False),
        sa.Column("number", sa.BigInteger(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("occurred_at", _TS, nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default=""),
        sa.Column("origin", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_rev_uuid", sa.Uuid(), nullable=False),
        sa.Column("retired_rev_uuid", sa.Uuid(), nullable=True),
        sa.Column("retired_at", _TS, nullable=True),
        sa.Column("superseded_by", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("instance_uuid", "number", name="uq_mem_entry_instance_uuid_number"),
        sa.CheckConstraint("status IN ('active','deleted')", name="ck_mem_entry_status"),
    )
    op.create_index("ix_mem_entry_topic", "mem_entry", ["topic_uuid", "status"])
    op.create_table(
        "mem_revision",
        *_row(),
        sa.Column("instance_uuid", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("topic_code", sa.String(64), nullable=True),
        sa.Column("removed", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("added", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("occurred_at", _TS, nullable=False),
        sa.Column("source", sa.String(32), nullable=True),
        sa.Column("origin", postgresql.JSONB(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("undoes_uuid", sa.Uuid(), nullable=True),
        sa.Column("undone_by_uuid", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.CheckConstraint(
            "kind IN ('write','undo','restore','move','split')", name="ck_mem_revision_kind"
        ),
    )
    op.create_index("ix_mem_revision_instance", "mem_revision", ["instance_uuid", "id"])
    op.create_index(
        "ux_mem_revision_idem",
        "mem_revision",
        ["instance_uuid", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("mem_revision")
    op.drop_table("mem_entry")
    op.drop_table("mem_topic")
    op.drop_table("mem_instance")
    op.drop_table("mem_scope")
