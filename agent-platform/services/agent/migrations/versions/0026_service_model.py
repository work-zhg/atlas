"""模型改由模型管理平台管（agent-platform/doc/design/model-design.html）：平台服务用哪个模型

Revision ID: 0026_service_model
Revises: 0025_memory_service
Create Date: 2026-10-28

service_model  会话标题、记忆补记用平台上的哪把客户端 Key（客户端类型 + 编码）、其中哪个模型。
               ★ 不预置：要先在模型管理平台建好 Key、分配模型，再在这里配（管理接口或命令行）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import TS_COL, UUID_COL, now_default

revision: str = "0026_service_model"
down_revision: str | Sequence[str] | None = "0025_memory_service"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_model",
        sa.Column("purpose", sa.String(32), primary_key=True),
        sa.Column("client_type", sa.String(64), nullable=False),
        sa.Column("client_code", sa.String(128), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("updated_by", UUID_COL, nullable=True),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
    )


def downgrade() -> None:
    op.drop_table("service_model")
