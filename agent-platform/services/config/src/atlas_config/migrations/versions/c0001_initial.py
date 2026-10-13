"""配置库初始表：技能、MCP 注册表、工具复核、审计

Revision ID: c0001_initial
Revises:
Create Date: 2026-10-05

不建外键（与运行时 0013 起的约定一致）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "skill",
        sa.Column("slug", sa.String(63), primary_key=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("origin_url", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.CheckConstraint(
            "source IN ('builtin','tenant','imported')", name=op.f("ck_skill_source_enum")
        ),
    )
    op.create_table(
        "skill_version",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("frontmatter", _JSON, nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(80), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("has_scripts", sa.Boolean(), nullable=False),
        sa.Column("files", _JSON, nullable=False),
        sa.Column("scan_result", _JSON, nullable=False),
        sa.Column("uploaded_by", sa.String(255), nullable=False),
        sa.Column("reviewed_by", sa.String(255), nullable=True),
        sa.Column("reviewed_at", _TS, nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("published_by", sa.String(255), nullable=True),
        sa.Column("published_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.UniqueConstraint("slug", "version", name="uq_skill_version_slug_version"),
        sa.CheckConstraint(
            "status IN ('pending_review','rejected','published','disabled','revoked')",
            name=op.f("ck_skill_version_status_enum"),
        ),
        sa.CheckConstraint(
            "version IS NOT NULL OR status IN ('pending_review','rejected')",
            name=op.f("ck_skill_version_released_has_version"),
        ),
    )
    op.create_index("ix_skill_version_slug_status", "skill_version", ["slug", "status"])

    op.create_table(
        "mcp_server",
        sa.Column("name", sa.String(24), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("transport", sa.String(24), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("headers", _JSON, nullable=False),
        sa.Column("credential_scope", sa.String(16), nullable=False),
        sa.Column("call_timeout_s", sa.Float(), nullable=True),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_by", sa.String(255), nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.CheckConstraint(
            "status IN ('enabled','disabled')", name=op.f("ck_mcp_server_status_enum")
        ),
        sa.CheckConstraint(
            "credential_scope IN ('platform','user')", name=op.f("ck_mcp_server_scope_enum")
        ),
        sa.CheckConstraint(
            "transport IN ('streamable_http','sse')", name=op.f("ck_mcp_server_transport_enum")
        ),
    )
    op.create_table(
        "mcp_tool_review",
        sa.Column("server", sa.String(24), primary_key=True),
        sa.Column("tool_name", sa.String(128), primary_key=True),
        sa.Column("digest", sa.String(80), primary_key=True),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("decided_by", sa.String(255), nullable=False),
        sa.Column("decided_at", _TS, nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved','rejected')", name=op.f("ck_mcp_tool_review_decision_enum")
        ),
    )
    op.create_table(
        "config_audit",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("at", _TS, nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_kind", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(255), nullable=False),
        sa.Column("detail", _JSON, nullable=False),
    )
    op.create_index("ix_config_audit_target", "config_audit", ["target_kind", "target_id", "at"])


def downgrade() -> None:
    op.drop_index("ix_config_audit_target", table_name="config_audit")
    op.drop_table("config_audit")
    op.drop_table("mcp_tool_review")
    op.drop_table("mcp_server")
    op.drop_index("ix_skill_version_slug_status", table_name="skill_version")
    op.drop_table("skill_version")
    op.drop_table("skill")
