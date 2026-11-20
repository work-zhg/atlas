"""多租户：四张表加 tenant_uuid；服务名改成租户内唯一

Revision ID: g0003
Revises: g0002
Create Date: 2026-11-19

c4-model/opscenter/design-tenant-isolation.html §8：MCP 服务由租户自己登记，「公有」只对本租户
公有；智能体 Key 只能用本租户的服务。查询按当前租户自动过滤（atlas_tenancy）。

Key 的原文、主体、持有人（用户中心 uid）、智能体编码都是全局唯一的，唯一约束不用改。

已有数据全部归原来的平台租户：环境变量 TENANT_BACKFILL_UUID（用户中心里
SELECT uuid FROM uc_tenant WHERE code = 'default'）。表里有数据却没给，迁移直接失败。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from atlas_tenancy.migration import add_tenant, drop_tenant, scope_unique, unscope_unique

revision: str = "g0003"
down_revision: str | Sequence[str] | None = "g0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("mcp_service", "mcp_tool", "mcp_api_key", "mcp_audit")
_OLD_UQ = "uq_mcp_service_name"
_NEW_UQ = "uq_mcp_service_tenant_uuid_name"


def _backfill() -> UUID:
    raw = os.environ.get("TENANT_BACKFILL_UUID", "").strip()
    if raw:
        return UUID(raw)
    bind = op.get_bind()
    for table in TABLES:
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError(
                f"表 {table} 里已有数据：设置 TENANT_BACKFILL_UUID 为原平台租户的 uuid 再迁移"
            )
    return UUID(int=0)


def upgrade() -> None:
    tenant = _backfill()
    for table in TABLES:
        add_tenant(op, table, backfill=tenant)
    scope_unique(op, "mcp_service", _OLD_UQ, ["name"], name=_NEW_UQ)


def downgrade() -> None:
    unscope_unique(op, "mcp_service", _NEW_UQ, _OLD_UQ, ["name"])
    for table in reversed(TABLES):
        drop_tenant(op, table)
