"""删 app_tenant_state、app_ops_request：业务系统不存租户状态，也不记运营请求

Revision ID: 0002_drop_tenant_state
Revises: 0001_opsapi
Create Date: 2026-11-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_appconfig.db import JSONType, UTCDateTime, UUIDType

revision: str = "0002_drop_tenant_state"
down_revision: str | Sequence[str] | None = "0001_opsapi"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_app_ops_request_tenant", table_name="app_ops_request")
    op.drop_table("app_ops_request")
    op.drop_table("app_tenant_state")


_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def downgrade() -> None:
    # 建回空表（状态和记录都不恢复），和 0001 建的一样
    op.create_table(
        "app_tenant_state",
        sa.Column("id", _ID, primary_key=True, autoincrement=True),
        sa.Column("tenant_uuid", UUIDType, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("operation_uuid", UUIDType, nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("changed_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_app_tenant_state"),
        sa.UniqueConstraint("tenant_uuid", name="uk_app_tenant_state_tenant"),
    )
    op.create_table(
        "app_ops_request",
        sa.Column("id", _ID, primary_key=True, autoincrement=True),
        sa.Column("idempotency_key", UUIDType, nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("tenant_uuid", UUIDType, nullable=True),
        sa.Column("operator_uuid", UUIDType, nullable=True),
        sa.Column("operator_name", sa.String(64), nullable=True),
        sa.Column("operation_uuid", UUIDType, nullable=True),
        sa.Column("result", JSONType, nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_app_ops_request"),
        sa.UniqueConstraint("idempotency_key", name="uk_app_ops_request_key"),
    )
    op.create_index("ix_app_ops_request_tenant", "app_ops_request", ["tenant_uuid", "created_at"])
