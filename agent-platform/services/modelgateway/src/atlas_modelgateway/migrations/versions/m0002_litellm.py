"""换成 LiteLLM + 模型管理平台（模型管理方案设计）：平台不再转发、不计量、没有平台用途。

删掉自研网关的表（模型目录、价目、探测、平台用途、主体 Key、调用记录、额度），
新建模型（只落基本信息）与 API Key（用户 Key / 客户端 Key）。审计表保留。

★ 旧数据不迁移：模型在平台上重新登记，Key 重新申请、分配。

Revision ID: m0002
Revises: m0001
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "m0002"
down_revision: str | Sequence[str] | None = "m0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_OLD = (
    "gw_budget",
    "gw_model_call",
    "gw_model_key",
    "gw_purpose",
    "gw_model_probe",
    "gw_model_price",
    "gw_model",
)


def _row() -> list[sa.Column]:
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("uuid", sa.Uuid(), nullable=False, unique=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
    ]


def upgrade() -> None:
    for table in _OLD:
        op.drop_table(table)

    op.create_table(
        "gw_model",
        *_row(),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("display_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="private"),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("litellm_model_id", sa.String(128), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(64), nullable=False, server_default=""),
        sa.CheckConstraint("visibility IN ('public','private')", name="ck_gw_model_visibility"),
    )

    op.create_table(
        "gw_api_key",
        *_row(),
        sa.Column("key_type", sa.String(16), nullable=False),
        sa.Column("user_uuid", sa.Uuid(), nullable=True),
        sa.Column("client_type", sa.String(64), nullable=True),
        sa.Column("client_code", sa.String(128), nullable=True),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("models", _JSON, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("secret", sa.String(128), nullable=True),
        sa.Column("litellm_token", sa.String(128), nullable=True),
        sa.Column("synced_models", _JSON, nullable=False),
        sa.Column("synced_blocked", sa.Boolean(), nullable=True),
        sa.Column("synced_at", _TS, nullable=True),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("rotated_at", _TS, nullable=True),
        sa.CheckConstraint("key_type IN ('user','client')", name="ck_gw_api_key_key_type"),
        sa.CheckConstraint("status IN ('pending','active','blocked')", name="ck_gw_api_key_status"),
        sa.CheckConstraint(
            "(key_type = 'user' AND user_uuid IS NOT NULL AND client_type IS NULL) OR "
            "(key_type = 'client' AND user_uuid IS NULL AND client_type IS NOT NULL "
            "AND client_code IS NOT NULL)",
            name="ck_gw_api_key_owner",
        ),
    )
    op.create_index(
        "uq_gw_api_key_user",
        "gw_api_key",
        ["user_uuid"],
        unique=True,
        postgresql_where=sa.text("key_type = 'user'"),
    )
    op.create_index(
        "uq_gw_api_key_client",
        "gw_api_key",
        ["client_type", "client_code"],
        unique=True,
        postgresql_where=sa.text("key_type = 'client'"),
    )


def downgrade() -> None:
    raise NotImplementedError("m0002 不支持降级：自研网关的表已删除，数据不可恢复")
