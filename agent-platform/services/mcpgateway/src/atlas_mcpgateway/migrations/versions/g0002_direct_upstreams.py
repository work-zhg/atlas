"""网关直连上游、Key 作为权限主体（MCP 网关原型）。

去掉：复核（mcp_tool_review）、访问日志（mcp_access_log）、Higress 上游与接入检查。
新增：服务类型（MCP 代理 / REST 转换 / gRPC 转换）、可见性、连接、上游凭据；工具的副作用、
映射、状态；Key 分用户 Key 与智能体 Key，每把在用户中心有一个主体。

★ 旧数据不迁：原来的服务都是 Higress 上的，定义和凭据在 Higress 里，换成直连后要重新登记；
  原来的 Key 是「人或数字员工」的身份 Key，授权在人身上，新方案授权在 Key 上，对不上。
  用户中心里旧的 McpServer 数据对象，重新登记同名服务时由网关清掉（registry.create）。

Revision ID: g0002
Revises: g0001
Create Date: 2026-10-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "g0002"
down_revision: str | Sequence[str] | None = "g0001"
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
    for table in ("mcp_access_log", "mcp_tool_review", "mcp_api_key", "mcp_tool", "mcp_service"):
        op.drop_table(table)

    op.create_table(
        "mcp_service",
        *_row(),
        sa.Column("name", sa.String(24), nullable=False, unique=True),
        sa.Column("display_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("visibility", sa.String(8), nullable=False, server_default="private"),
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
        sa.CheckConstraint("kind IN ('mcp','rest','grpc')", name="kind"),
        sa.CheckConstraint("visibility IN ('public','private')", name="visibility"),
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
        sa.Column("effect", sa.String(16), nullable=False, server_default="write"),
        sa.Column("mapping", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("issues", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("previous", postgresql.JSONB(), nullable=True),
        sa.Column("changed_at", _TS, nullable=True),
        sa.Column("removed_at", _TS, nullable=True),
        sa.Column("uc_registered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("service_uuid", "name"),
        sa.CheckConstraint("effect IN ('read','write','destructive')", name="effect"),
        sa.CheckConstraint("status IN ('active','disabled')", name="status"),
    )
    op.create_table(
        "mcp_api_key",
        *_row(),
        sa.Column("key_type", sa.String(16), nullable=False),
        sa.Column("user_uuid", sa.Uuid(), nullable=True),
        sa.Column("client_type", sa.String(64), nullable=True),
        sa.Column("client_code", sa.String(128), nullable=True),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("subject_uid", sa.Uuid(), nullable=False, unique=True),
        sa.Column("secret", sa.String(80), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("rotated_at", _TS, nullable=False),
        sa.CheckConstraint("key_type IN ('user','client')", name="key_type"),
        sa.CheckConstraint("status IN ('active','blocked')", name="status"),
        sa.CheckConstraint(
            "(key_type = 'user') = (user_uuid IS NOT NULL)"
            " AND (key_type = 'client') = (client_type IS NOT NULL AND client_code IS NOT NULL)",
            name="owner",
        ),
    )
    op.create_index(
        "uq_mcp_api_key_user",
        "mcp_api_key",
        ["user_uuid"],
        unique=True,
        postgresql_where=sa.text("key_type = 'user'"),
    )
    op.create_index(
        "uq_mcp_api_key_client",
        "mcp_api_key",
        ["client_type", "client_code"],
        unique=True,
        postgresql_where=sa.text("key_type = 'client'"),
    )


def downgrade() -> None:
    raise NotImplementedError("g0002 丢弃了 Higress 时代的服务和 Key，不能降级")
