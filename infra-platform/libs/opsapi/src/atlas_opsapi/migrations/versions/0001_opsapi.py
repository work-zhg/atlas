"""app_tenant_state、app_ops_request

Revision ID: 0001_opsapi
Revises:
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_appconfig.db import JSONType, UTCDateTime, UUIDType

revision: str = "0001_opsapi"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_table("app_ops_request")
    op.drop_table("app_tenant_state")
