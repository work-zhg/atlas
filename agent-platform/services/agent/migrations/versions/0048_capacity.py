"""容量：租户份额 tenant_capacity、每人存储用量 storage_usage

Revision ID: 0048_capacity
Revises: 0047_tenant
Create Date: 2026-11-19

c4-model/agent/code-capacity.html。共用设施分三层管：总容量（部署的执行能力）、租户份额（运营中心
经运营接口定，存这里）、每人上限（租户设置，在应用配置库里）。两张都是租户表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import TS_COL, UUID_COL, now_default

revision: str = "0048_capacity"
down_revision: str | Sequence[str] | None = "0047_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_capacity",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_uuid", UUID_COL, nullable=False),
        sa.Column("running_runs", sa.Integer(), nullable=True),
        sa.Column("sandboxes", sa.Integer(), nullable=True),
        sa.Column("storage_bytes", sa.BigInteger(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_by", sa.String(64), nullable=False, server_default="system"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_capacity"),
        sa.UniqueConstraint("tenant_uuid", name="uk_tenant_capacity_tenant"),
    )
    op.create_index("ix_tenant_capacity_tenant_uuid", "tenant_capacity", ["tenant_uuid"])
    op.create_table(
        "storage_usage",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_uuid", UUID_COL, nullable=False),
        sa.Column("user_uuid", UUID_COL, nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("measured_at", TS_COL, nullable=False, server_default=now_default()),
        sa.PrimaryKeyConstraint("id", name="pk_storage_usage"),
        sa.UniqueConstraint("tenant_uuid", "user_uuid", name="uk_storage_usage_user"),
    )
    op.create_index("ix_storage_usage_tenant_uuid", "storage_usage", ["tenant_uuid"])


def downgrade() -> None:
    op.drop_table("storage_usage")
    op.drop_table("tenant_capacity")
