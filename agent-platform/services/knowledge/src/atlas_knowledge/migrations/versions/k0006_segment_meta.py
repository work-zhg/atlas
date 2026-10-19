"""分段级元数据覆盖（doc/knowledge-enterprise-design.html §7.2）

Revision ID: k0006_segment_meta
Revises: k0005_index_versions
Create Date: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "k0006_segment_meta"
down_revision: str | Sequence[str] | None = "k0005_index_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("kb_segment", sa.Column("meta", _JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("kb_segment", "meta")
