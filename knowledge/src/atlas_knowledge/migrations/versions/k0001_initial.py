"""知识库初始表：知识库、成员、处理规则、文档、分段、子块、向量缓存、检索记录

Revision ID: k0001_initial
Revises:
Create Date: 2026-10-11

自增 id + uuid 逻辑主键；不建外键。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "k0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_TS = sa.DateTime(timezone=True)


def _base(name: str) -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("uuid", sa.Uuid(), nullable=False, unique=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "kb_dataset",
        *_base("kb_dataset"),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("icon", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("permission", sa.String(32), nullable=False),
        sa.Column("indexing_technique", sa.String(32), nullable=False),
        sa.Column("embedding_provider", sa.String(64), nullable=False),
        sa.Column("embedding_model", sa.String(255), nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.Column("retrieval_config", _JSON, nullable=False),
        sa.Column("doc_form", sa.String(32), nullable=False),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("collection_name", sa.String(128), nullable=False),
        sa.Column("latest_process_rule_uuid", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
    )
    op.create_index(op.f("ix_kb_dataset_owner_id"), "kb_dataset", ["owner_id"])

    op.create_table(
        "kb_dataset_permission",
        *_base("kb_dataset_permission"),
        sa.Column("dataset_uuid", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(64), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.UniqueConstraint(
            "dataset_uuid",
            "principal_id",
            name=op.f("uq_kb_dataset_permission_dataset_uuid_principal_id"),
        ),
    )
    op.create_index(
        op.f("ix_kb_dataset_permission_dataset_uuid"), "kb_dataset_permission", ["dataset_uuid"]
    )

    op.create_table(
        "kb_process_rule",
        *_base("kb_process_rule"),
        sa.Column("dataset_uuid", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("rules", _JSON, nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
    )
    op.create_index(op.f("ix_kb_process_rule_dataset_uuid"), "kb_process_rule", ["dataset_uuid"])

    op.create_table(
        "kb_document",
        *_base("kb_document"),
        sa.Column("dataset_uuid", sa.Uuid(), nullable=False),
        sa.Column("process_rule_uuid", sa.Uuid(), nullable=False),
        sa.Column("batch", sa.String(64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("data_source_type", sa.String(32), nullable=False),
        sa.Column("data_source_info", _JSON, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("doc_form", sa.String(32), nullable=False),
        sa.Column("doc_language", sa.String(32), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("indexing_status", sa.String(16), nullable=False),
        sa.Column("processing_started_at", _TS, nullable=True),
        sa.Column("parsing_completed_at", _TS, nullable=True),
        sa.Column("splitting_completed_at", _TS, nullable=True),
        sa.Column("completed_at", _TS, nullable=True),
        sa.Column("paused_at", _TS, nullable=True),
        sa.Column("paused_by", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("indexing_latency", sa.Float(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("review_status", sa.String(16), nullable=False),
        sa.Column("reviewed_by", sa.String(64), nullable=True),
        sa.Column("reviewed_at", _TS, nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
    )
    op.create_index(
        op.f("ix_kb_document_dataset_uuid_position"), "kb_document", ["dataset_uuid", "position"]
    )

    op.create_table(
        "kb_segment",
        *_base("kb_segment"),
        sa.Column("dataset_uuid", sa.Uuid(), nullable=False),
        sa.Column("document_uuid", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("keywords", _JSON, nullable=True),
        sa.Column("index_node_id", sa.String(64), nullable=True),
        sa.Column("index_node_hash", sa.String(64), nullable=True),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=True),
    )
    op.create_index(op.f("ix_kb_segment_dataset_uuid"), "kb_segment", ["dataset_uuid"])
    op.create_index(
        op.f("ix_kb_segment_document_uuid_position"), "kb_segment", ["document_uuid", "position"]
    )

    op.create_table(
        "kb_child_chunk",
        *_base("kb_child_chunk"),
        sa.Column("dataset_uuid", sa.Uuid(), nullable=False),
        sa.Column("document_uuid", sa.Uuid(), nullable=False),
        sa.Column("segment_uuid", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("index_node_id", sa.String(64), nullable=True),
        sa.Column("index_node_hash", sa.String(64), nullable=True),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
    )
    op.create_index(op.f("ix_kb_child_chunk_document_uuid"), "kb_child_chunk", ["document_uuid"])
    op.create_index(
        op.f("ix_kb_child_chunk_segment_uuid_position"),
        "kb_child_chunk",
        ["segment_uuid", "position"],
    )

    op.create_table(
        "kb_embedding_cache",
        *_base("kb_embedding_cache"),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint(
            "provider",
            "model",
            "text_hash",
            name=op.f("uq_kb_embedding_cache_provider_model_text_hash"),
        ),
    )

    op.create_table(
        "kb_query_log",
        *_base("kb_query_log"),
        sa.Column("dataset_uuid", sa.Uuid(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(128), nullable=True),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
    )
    op.create_index(op.f("ix_kb_query_log_dataset_uuid"), "kb_query_log", ["dataset_uuid"])


def downgrade() -> None:
    for table in (
        "kb_query_log",
        "kb_embedding_cache",
        "kb_child_chunk",
        "kb_segment",
        "kb_document",
        "kb_process_rule",
        "kb_dataset_permission",
        "kb_dataset",
    ):
        op.drop_table(table)
