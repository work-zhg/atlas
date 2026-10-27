"""MCP 网关的初始表（doc/mcp-design.html 第二版）。

Revision ID: g0001
Revises:
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "g0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)


def _row() -> list[sa.Column]:
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("uuid", sa.Uuid(), nullable=False, unique=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "mcp_service",
        *_row(),
        sa.Column("name", sa.String(24), nullable=False, unique=True),
        sa.Column("display_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("upstream_kind", sa.String(16), nullable=False, server_default="higress"),
        sa.Column("upstream_name", sa.String(128), nullable=False),
        sa.Column("credential_mode", sa.String(16), nullable=False, server_default="subject_key"),
        sa.Column("review_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("call_timeout_s", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="enabled"),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("fetched_at", _TS, nullable=True),
        sa.Column("fetch_error", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("changed_at", _TS, nullable=True),
        sa.Column("refresh_claimed_at", _TS, nullable=True),
        sa.Column("access_checked_at", _TS, nullable=True),
        sa.Column("access_check_ok", sa.Boolean(), nullable=True),
        sa.Column("access_check_detail", sa.Text(), nullable=True),
        sa.CheckConstraint("upstream_kind IN ('higress')", name="upstream_kind"),
        sa.CheckConstraint(
            "credential_mode IN ('subject_key','user_oauth')", name="credential_mode"
        ),
        sa.CheckConstraint("status IN ('enabled','disabled')", name="status"),
    )
    op.create_table(
        "mcp_tool",
        *_row(),
        sa.Column("service_uuid", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("input_schema", postgresql.JSONB(), nullable=False),
        sa.Column("annotations", postgresql.JSONB(), nullable=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("issues", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("previous", postgresql.JSONB(), nullable=True),
        sa.Column("changed_at", _TS, nullable=True),
        sa.Column("removed_at", _TS, nullable=True),
        sa.UniqueConstraint("service_uuid", "name"),
    )
    op.create_table(
        "mcp_tool_review",
        *_row(),
        sa.Column("service_uuid", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("reviewed_by", sa.String(64), nullable=False),
        sa.UniqueConstraint("service_uuid", "tool_name", "digest"),
        sa.CheckConstraint("decision IN ('approved','rejected')", name="decision"),
    )
    op.create_table(
        "mcp_api_key",
        *_row(),
        sa.Column("subject_uid", sa.Uuid(), nullable=False, unique=True),
        sa.Column("subject_type", sa.String(16), nullable=False, server_default="human"),
        sa.Column("subject_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("api_key", sa.String(80), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("issued_by", sa.String(64), nullable=False),
        sa.Column("rotated_at", _TS, nullable=False),
        sa.Column("disabled_at", _TS, nullable=True),
        sa.CheckConstraint("status IN ('active','disabled')", name="status"),
    )
    op.create_table(
        "mcp_access_log",
        *_row(),
        sa.Column("service", sa.String(64), nullable=False),
        sa.Column("subject_uid", sa.Uuid(), nullable=True),
        sa.Column("user_uid", sa.String(64), nullable=True),
        sa.Column("caller_app", sa.String(64), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("http_method", sa.String(8), nullable=False),
        sa.Column("rpc_method", sa.String(64), nullable=True),
        sa.Column("tool", sa.String(128), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("forwarded", sa.Boolean(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_mcp_access_log_service_at", "mcp_access_log", ["service", "created_at"])
    op.create_index("ix_mcp_access_log_subject_at", "mcp_access_log", ["subject_uid", "created_at"])
    op.create_table(
        "mcp_audit",
        *_row(),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(200), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_mcp_audit_target_at", "mcp_audit", ["target", "created_at"])


def downgrade() -> None:
    for table in (
        "mcp_audit",
        "mcp_access_log",
        "mcp_api_key",
        "mcp_tool_review",
        "mcp_tool",
        "mcp_service",
    ):
        op.drop_table(table)
