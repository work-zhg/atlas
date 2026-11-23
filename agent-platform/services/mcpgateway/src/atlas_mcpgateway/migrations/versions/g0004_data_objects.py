"""授权自己记登记过的数据对象：mcp_data_object 取代 mcp_tool.uc_registered

Revision ID: g0004
Revises: g0003
Create Date: 2026-11-23

c4-model/mcpgateway/code-access.html：在用户中心登记过哪些数据对象，由授权领域记在自己的表里，
服务目录的表不再带授权的标记。

回填：每个服务一条 McpServer（登记服务时就在用户中心登记了）；uc_registered 为真的工具各一条
McpTool。用户中心里的数据不动。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_mcpgateway.db.types import uuid7

revision: str = "g0004"
down_revision: str | Sequence[str] | None = "g0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "mcp_data_object",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("uuid", sa.Uuid(), nullable=False, unique=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.Column("tenant_uuid", sa.Uuid(), nullable=False),
        sa.Column("data_code", sa.String(16), nullable=False),
        sa.Column("data_id", sa.String(160), nullable=False),
        sa.Column("service_uuid", sa.Uuid(), nullable=False),
        sa.UniqueConstraint("tenant_uuid", "data_code", "data_id"),
        sa.CheckConstraint("data_code IN ('McpServer','McpTool')", name="data_code"),
    )
    op.create_index(None, "mcp_data_object", ["tenant_uuid"])
    op.create_index(None, "mcp_data_object", ["service_uuid"])

    bind = op.get_bind()
    rows = [
        (r.tenant_uuid, "McpServer", r.name, r.uuid)
        for r in bind.execute(sa.text("SELECT uuid, tenant_uuid, name FROM mcp_service"))
    ]
    rows += [
        (r.tenant_uuid, "McpTool", f"{r.service}/{r.tool}", r.service_uuid)
        for r in bind.execute(
            sa.text(
                "SELECT t.tenant_uuid, t.service_uuid, t.name AS tool, s.name AS service"
                " FROM mcp_tool t JOIN mcp_service s ON s.uuid = t.service_uuid"
                " WHERE t.uc_registered"
            )
        )
    ]
    if rows:
        bind.execute(
            sa.text(
                "INSERT INTO mcp_data_object"
                " (uuid, created_at, updated_at, tenant_uuid, data_code, data_id, service_uuid)"
                " VALUES (:uuid, now(), now(), :tenant, :code, :data_id, :service)"
            ),
            [
                {"uuid": uuid7(), "tenant": t, "code": c, "data_id": d, "service": s}
                for t, c, d, s in rows
            ],
        )
    op.drop_column("mcp_tool", "uc_registered")


def downgrade() -> None:
    op.add_column(
        "mcp_tool",
        sa.Column("uc_registered", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        "UPDATE mcp_tool t SET uc_registered = true FROM mcp_data_object o, mcp_service s"
        " WHERE o.data_code = 'McpTool' AND s.uuid = t.service_uuid"
        " AND o.data_id = s.name || '/' || t.name"
    )
    op.drop_table("mcp_data_object")
