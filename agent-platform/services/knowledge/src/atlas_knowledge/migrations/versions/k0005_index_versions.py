"""索引版本、文档元数据 / 权限 / 版本与时效（doc/knowledge-enterprise-design.html §6.2 / §7.2–7.5）

Revision ID: k0005_index_versions
Revises: k0004_docir
Create Date: 2026-10-14

已有知识库补一条 version=1 的 active 版本（就是它当前的模型与 collection）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "k0005_index_versions"
down_revision: str | Sequence[str] | None = "k0004_docir"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_TS = sa.DateTime(timezone=True)


def _base() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("uuid", sa.Uuid(), nullable=False, unique=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "kb_index_version",
        *_base(),
        sa.Column("dataset_uuid", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("embedding_provider", sa.String(64), nullable=False),
        sa.Column("embedding_model", sa.String(255), nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.Column("collection_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("auto_activate", sa.Boolean(), nullable=False),
        sa.Column("progress", _JSON, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("activated_at", _TS, nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "dataset_uuid", "version", name=op.f("uq_kb_index_version_dataset_uuid_version")
        ),
    )
    op.create_index(
        op.f("ix_kb_index_version_dataset_uuid"), "kb_index_version", ["dataset_uuid"]
    )
    op.execute(
        """
        INSERT INTO kb_index_version (uuid, created_at, updated_at, dataset_uuid, version,
            embedding_provider, embedding_model, embedding_dim, collection_name, status,
            auto_activate, activated_at, created_by)
        SELECT gen_random_uuid(), now(), now(), uuid, 1, embedding_provider, embedding_model,
            embedding_dim, collection_name, 'active', false, now(), created_by
        FROM kb_dataset
        """
    )

    op.create_table(
        "kb_metadata_field",
        *_base(),
        sa.Column("dataset_uuid", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("options", _JSON, nullable=True),
        sa.Column("multi", sa.Boolean(), nullable=False),
        sa.Column("llm_extract", sa.Boolean(), nullable=False),
        sa.UniqueConstraint(
            "dataset_uuid", "name", name=op.f("uq_kb_metadata_field_dataset_uuid_name")
        ),
    )
    op.create_index(
        op.f("ix_kb_metadata_field_dataset_uuid"), "kb_metadata_field", ["dataset_uuid"]
    )

    op.add_column("kb_document", sa.Column("doc_metadata", _JSON, nullable=True))
    op.add_column("kb_document", sa.Column("acl", _JSON, nullable=True))
    op.add_column("kb_document", sa.Column("version_group", sa.Uuid(), nullable=True))
    op.add_column(
        "kb_document",
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("kb_document", sa.Column("valid_from", _TS, nullable=True))
    op.add_column("kb_document", sa.Column("valid_to", _TS, nullable=True))
    op.create_index(op.f("ix_kb_document_version_group"), "kb_document", ["version_group"])


def downgrade() -> None:
    op.drop_index(op.f("ix_kb_document_version_group"), table_name="kb_document")
    for col in ("valid_to", "valid_from", "is_current", "version_group", "acl", "doc_metadata"):
        op.drop_column("kb_document", col)
    op.drop_table("kb_metadata_field")
    op.drop_table("kb_index_version")
