"""知识库文件计入租户的存储份额（多租户设计 §9）

Revision ID: k0010_capacity
Revises: k0009_tenant
Create Date: 2026-11-20

    kb_document.file_size     原文件的字节数（算用量）；已有的从 data_source_info.size 回填
    kb_tenant_capacity        运营中心单独给某个租户定的份额（没有行 = 用默认份额）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0010_capacity"
down_revision: str | Sequence[str] | None = "k0009_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "kb_document",
        sa.Column("file_size", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.execute(
        """
        UPDATE kb_document SET file_size = (data_source_info ->> 'size')::bigint
        WHERE data_source_info ->> 'size' ~ '^[0-9]+$'
        """
    )
    op.alter_column("kb_document", "file_size", server_default=None)
    op.create_table(
        "kb_tenant_capacity",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("uuid", sa.Uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_uuid", sa.Uuid, nullable=False),
        sa.Column("storage_bytes", sa.BigInteger, nullable=True),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_kb_tenant_capacity"),
        sa.UniqueConstraint("uuid", name="uq_kb_tenant_capacity_uuid"),
        sa.UniqueConstraint("tenant_uuid", name="uq_kb_tenant_capacity_tenant_uuid"),
    )
    op.create_index("ix_kb_tenant_capacity_tenant_uuid", "kb_tenant_capacity", ["tenant_uuid"])


def downgrade() -> None:
    op.drop_table("kb_tenant_capacity")
    op.drop_column("kb_document", "file_size")
