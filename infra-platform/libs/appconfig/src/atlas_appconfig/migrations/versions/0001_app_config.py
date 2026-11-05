"""app_config、app_config_revision

Revision ID: 0001_app_config
Revises:
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_appconfig.db import JSONType, UTCDateTime, UUIDType

revision: str = "0001_app_config"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_config",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("config_uuid", UUIDType, nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value_kind", sa.String(16), nullable=False),
        sa.Column("element_kind", sa.String(16), nullable=True),
        sa.Column("value", JSONType, nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_app_config"),
        sa.UniqueConstraint("config_uuid", name="uk_app_config_uuid"),
        sa.UniqueConstraint("key", name="uk_app_config_key"),
    )
    op.create_table(
        "app_config_revision",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("revision_uuid", UUIDType, nullable=False),
        sa.Column("config_uuid", UUIDType, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("value", JSONType, nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_app_config_revision"),
        sa.UniqueConstraint("revision_uuid", name="uk_app_config_revision_uuid"),
        sa.UniqueConstraint("config_uuid", "version", name="uk_app_config_revision_version"),
    )


def downgrade() -> None:
    op.drop_table("app_config_revision")
    op.drop_table("app_config")
