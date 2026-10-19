"""文档理解与分段增强：DocIR、解析质量报告、分段引用定位、向量化前缀、增强开关
（doc/knowledge-enterprise-design.html §4 / §5）

Revision ID: k0004_docir
Revises: k0003_retrieval_trace
Create Date: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "k0004_docir"
down_revision: str | Sequence[str] | None = "k0003_retrieval_trace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("kb_document", sa.Column("parse_report", _JSON, nullable=True))
    op.add_column("kb_document", sa.Column("docir_key", sa.String(length=512), nullable=True))
    op.add_column("kb_segment", sa.Column("source_locator", _JSON, nullable=True))
    op.add_column("kb_segment", sa.Column("context", sa.Text(), nullable=True))
    op.add_column("kb_dataset", sa.Column("enhancements", _JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("kb_dataset", "enhancements")
    op.drop_column("kb_segment", "context")
    op.drop_column("kb_segment", "source_locator")
    op.drop_column("kb_document", "docir_key")
    op.drop_column("kb_document", "parse_report")
