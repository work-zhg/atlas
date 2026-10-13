"""检索过程记录：kb_retrieval，kb_query_log 指回它

Revision ID: k0003_retrieval_trace
Revises: k0002_structure_chunking
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "k0003_retrieval_trace"
down_revision: str | Sequence[str] | None = "k0002_structure_chunking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "kb_retrieval",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("uuid", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dataset_uuids", _JSON, nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(128), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("config", _JSON, nullable=False),
        sa.Column("stages", _JSON, nullable=False),
        sa.Column("results", _JSON, nullable=False),
        sa.Column("trace_id", sa.String(32), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_kb_retrieval"),
        sa.UniqueConstraint("uuid", name="uq_kb_retrieval_uuid"),
    )
    op.add_column("kb_query_log", sa.Column("retrieval_uuid", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("kb_query_log", "retrieval_uuid")
    op.drop_table("kb_retrieval")
