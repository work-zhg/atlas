"""MCP 注册表与工具复核迁到 MCP 网关（doc/mcp-design.html 第二版 §2.1）：删掉配置库里的两张表

Revision ID: c0003_drop_mcp
Revises: c0002_instruction
Create Date: 2026-10-27

★ 注册表、工具定义、复核结论、API Key 现在都归 MCP 网关（atlas_mcpgateway）。Atlas 一直用的是
  MCP_SERVERS（env 方式），这两张表在本机与 112 上都没有数据；原来的服务由
  `python -m atlas_mcpgateway import --from-env …` 登记进网关（deploy/server/mcp-cutover.sh）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c0003_drop_mcp"
down_revision: str | Sequence[str] | None = "c0002_instruction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.drop_table("mcp_tool_review")
    op.drop_table("mcp_server")


def downgrade() -> None:
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
