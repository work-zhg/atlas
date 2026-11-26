"""入库增强的结果缓存从 Redis 挪到数据库：kb_enhance_cache（知识库重构清单 K2）

Revision ID: k0012_enhance_cache
Revises: k0011_c4_target
Create Date: 2026-11-25

Redis 里的旧缓存（kb:enh:*）不搬：丢了只是重跑入库时多调一次模型。部署后在 Redis 3 号库删掉它们，
一来马上释放内存，二来兑现「删除租户就删掉它的数据」（旧键里没有租户，删租户时清不掉）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0012_enhance_cache"
down_revision: str | Sequence[str] | None = "k0011_c4_target"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kb_enhance_cache",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("uuid", sa.Uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_uuid", sa.Uuid, nullable=False),
        sa.Column("cache_key", sa.String(512), nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_kb_enhance_cache"),
        sa.UniqueConstraint("uuid", name="uq_kb_enhance_cache_uuid"),
        sa.UniqueConstraint(
            "tenant_uuid", "cache_key", name="uq_kb_enhance_cache_tenant_uuid_cache_key"
        ),
    )
    op.create_index("ix_kb_enhance_cache_tenant_uuid", "kb_enhance_cache", ["tenant_uuid"])
    op.create_index("ix_kb_enhance_cache_created_at", "kb_enhance_cache", ["created_at"])


def downgrade() -> None:
    op.drop_table("kb_enhance_cache")
