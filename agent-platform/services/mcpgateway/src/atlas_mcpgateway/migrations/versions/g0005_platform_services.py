"""平台服务：mcp_platform_service、mcp_platform_tool

Revision ID: g0005
Revises: g0004
Create Date: 2026-11-25

c4-model/mcpgateway/code-catalog.html：平台自己的服务（例如知识库）登记一次，所有租户可见，只能由
运维命令行登记。列和 mcp_service、mcp_tool 一样，只是没有 tenant_uuid —— 租户隔离库要求租户表的
tenant_uuid 非空、查询都按租户过滤，所以单独两张表，而不是在租户表里留空租户。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "g0005"
down_revision: str | Sequence[str] | None = "g0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)


def _row() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("uuid", sa.Uuid(), nullable=False, unique=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "mcp_platform_service",
        *_row(),
        sa.Column("name", sa.String(24), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("visibility", sa.String(8), nullable=False, server_default="public"),
        sa.Column("connection", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("credential", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("credential_secret", sa.Text(), nullable=True),
        sa.Column("descriptor", sa.LargeBinary(), nullable=True),
        sa.Column("pass_identity", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(16), nullable=False, server_default="enabled"),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("fetched_at", _TS, nullable=True),
        sa.Column("fetch_error", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("changed_at", _TS, nullable=True),
        sa.Column("refresh_claimed_at", _TS, nullable=True),
        sa.UniqueConstraint("name"),
        sa.CheckConstraint("kind IN ('mcp','rest','grpc')", name="kind"),
        sa.CheckConstraint("visibility IN ('public','private')", name="visibility"),
        sa.CheckConstraint("status IN ('enabled','disabled')", name="status"),
    )
    op.create_table(
        "mcp_platform_tool",
        *_row(),
        sa.Column("service_uuid", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("input_schema", postgresql.JSONB(), nullable=False),
        sa.Column("annotations", postgresql.JSONB(), nullable=True),
        sa.Column("effect", sa.String(16), nullable=False, server_default="write"),
        sa.Column("mapping", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("issues", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("previous", postgresql.JSONB(), nullable=True),
        sa.Column("changed_at", _TS, nullable=True),
        sa.Column("removed_at", _TS, nullable=True),
        sa.UniqueConstraint("service_uuid", "name"),
        sa.CheckConstraint("effect IN ('read','write','destructive')", name="effect"),
        sa.CheckConstraint("status IN ('active','disabled')", name="status"),
    )


def downgrade() -> None:
    op.drop_table("mcp_platform_tool")
    op.drop_table("mcp_platform_service")
