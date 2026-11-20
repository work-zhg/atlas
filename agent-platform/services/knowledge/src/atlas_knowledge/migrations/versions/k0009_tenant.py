"""多租户：业务表加 tenant_uuid；库记下自己在对象存储里的目录

Revision ID: k0009_tenant
Revises: k0008_knowledge_sets
Create Date: 2026-11-20

c4-model/opscenter/design-tenant-isolation.html §8：知识库、知识集由租户自己建，「公有」只对本租户
公有。查询按当前租户自动过滤（atlas_tenancy）。

    11 张表加 tenant_uuid       向量缓存 kb_embedding_cache 除外（纯函数缓存，不归租户）
    kb_dataset.storage_prefix   库的文件目录：之后新建的放在 {租户}/_knowledge/{库}，已有的库回填
                                原来的 _knowledge/{库}（对象原地不动）

唯一约束都带着库 id（全局唯一），不用改成租户内唯一。

已有数据全部归原来的平台租户：环境变量 TENANT_BACKFILL_UUID（用户中心里
SELECT uuid FROM uc_tenant WHERE code = 'default'）。表里有数据却没给，迁移直接失败。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from atlas_tenancy.migration import add_tenant, drop_tenant

revision: str = "k0009_tenant"
down_revision: str | Sequence[str] | None = "k0008_knowledge_sets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "kb_knowledge_set",
    "kb_dataset",
    "kb_dataset_permission",
    "kb_process_rule",
    "kb_document",
    "kb_segment",
    "kb_child_chunk",
    "kb_query_log",
    "kb_retrieval",
    "kb_index_version",
    "kb_metadata_field",
)
#: 多租户之前的对象前缀（settings.oss_prefix 的默认值，部署里没改过）
_LEGACY_PREFIX = "_knowledge"


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
    op.add_column("kb_dataset", sa.Column("storage_prefix", sa.String(255), nullable=True))
    op.execute(
        sa.text("UPDATE kb_dataset SET storage_prefix = :p || '/' || uuid::text").bindparams(
            p=_LEGACY_PREFIX
        )
    )
    op.alter_column("kb_dataset", "storage_prefix", existing_type=sa.String(255), nullable=False)


def downgrade() -> None:
    op.drop_column("kb_dataset", "storage_prefix")
    for table in reversed(TABLES):
        drop_tenant(op, table)
