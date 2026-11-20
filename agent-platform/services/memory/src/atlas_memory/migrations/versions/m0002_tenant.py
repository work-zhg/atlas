"""多租户：作用域实例、主题、条目、修订加 tenant_uuid；实例的「作用域 + 关联主键」改成租户内唯一

Revision ID: m0002
Revises: m0001
Create Date: 2026-11-19

c4-model/opscenter/design-tenant-isolation.html。作用域（mem_scope）是产品自带的定义，所有租户
共用，不加租户；数据（实例及以下）归租户，查询按当前租户自动过滤（atlas_tenancy）。

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

revision: str = "m0002"
down_revision: str | Sequence[str] | None = "m0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("mem_instance", "mem_topic", "mem_entry", "mem_revision")
_OLD_UQ = "uq_mem_instance_scope_uuid_key_text"
_NEW_UQ = "uq_mem_instance_tenant_uuid_scope_uuid_key_text"


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
    scope_unique(op, "mem_instance", _OLD_UQ, ["scope_uuid", "key_text"], name=_NEW_UQ)


def downgrade() -> None:
    unscope_unique(op, "mem_instance", _NEW_UQ, _OLD_UQ, ["scope_uuid", "key_text"])
    for table in reversed(TABLES):
        drop_tenant(op, table)
