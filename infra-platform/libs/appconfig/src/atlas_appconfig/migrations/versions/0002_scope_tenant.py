"""app_config：作用域（系统参数 / 租户设置）和租户的值

Revision ID: 0002_scope_tenant
Revises: 0001_app_config
Create Date: 2026-11-19

已有的行都是系统参数（scope = system）的系统值（tenant_uuid = 全零 UUID，见 db.py）。
唯一约束从 key 换成 (key, tenant_uuid)：一个 key 一行系统值，每个租户至多一行。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_appconfig.db import SYSTEM_ROW, UUIDType

revision: str = "0002_scope_tenant"
down_revision: str | Sequence[str] | None = "0001_app_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_config",
        sa.Column("scope", sa.String(16), nullable=False, server_default="system"),
    )
    op.add_column("app_config", sa.Column("tenant_uuid", UUIDType, nullable=True))
    t = sa.table("app_config", sa.column("tenant_uuid", UUIDType))
    op.execute(t.update().values(tenant_uuid=SYSTEM_ROW))
    op.alter_column("app_config", "tenant_uuid", existing_type=UUIDType, nullable=False)
    op.alter_column("app_config", "scope", existing_type=sa.String(16), server_default=None)
    op.drop_constraint("uk_app_config_key", "app_config", type_="unique")
    op.create_unique_constraint("uk_app_config_key_tenant", "app_config", ["key", "tenant_uuid"])


def downgrade() -> None:
    # 租户的值（不是系统值的那些行）和它们的修订都丢掉：旧版本没有地方放它们
    t = sa.table(
        "app_config", sa.column("config_uuid", UUIDType), sa.column("tenant_uuid", UUIDType)
    )
    r = sa.table("app_config_revision", sa.column("config_uuid", UUIDType))
    tenant_rows = sa.select(t.c.config_uuid).where(t.c.tenant_uuid != SYSTEM_ROW)
    op.execute(r.delete().where(r.c.config_uuid.in_(tenant_rows)))
    op.execute(t.delete().where(t.c.tenant_uuid != SYSTEM_ROW))
    op.drop_constraint("uk_app_config_key_tenant", "app_config", type_="unique")
    op.create_unique_constraint("uk_app_config_key", "app_config", ["key"])
    op.drop_column("app_config", "tenant_uuid")
    op.drop_column("app_config", "scope")
