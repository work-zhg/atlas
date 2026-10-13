"""按标题分段：分段的标题路径、子块的向量前缀

Revision ID: k0002_structure_chunking
Revises: k0001_initial
Create Date: 2026-10-12

两列都可空：老数据没有标题路径，建向量时按原样处理。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "k0002_structure_chunking"
down_revision: str | Sequence[str] | None = "k0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("kb_segment", sa.Column("title_path", _JSON, nullable=True))
    op.add_column("kb_child_chunk", sa.Column("context", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("kb_child_chunk", "context")
    op.drop_column("kb_segment", "title_path")
