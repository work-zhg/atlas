"""多租户：技能归租户（slug、版本号在租户内）；新增技能市场（产品自带、不分租户）

Revision ID: c0005_tenant_market
Revises: c0004_skill_app
Create Date: 2026-11-19

c4-model/opscenter/design-tenant-isolation.html §8：技能由租户自己上传、本租户的审核人审核，
版本号在各租户内各自编排。用户 11-19 定：产品自带的技能（浏览器、computer use）放进「技能市场」，
租户从市场拷贝；创建租户时默认拷贝标了「默认安装」的。

    skill / skill_version / skill_audit   加 tenant_uuid；skill 的主键 slug → (tenant_uuid, slug)；
                                          版本唯一 (slug, version) → (tenant_uuid, slug, version)；
                                          来源加 market（从市场拷来的）
    market_skill / market_skill_version   技能市场：不属于任何租户，只能经运营接口发布

已有技能全部归原来的平台租户：环境变量 TENANT_BACKFILL_UUID（用户中心里
SELECT uuid FROM uc_tenant WHERE code = 'default'）。表里有数据却没给，迁移直接失败。
已发布版本的对象原地不动（storage_key 记着在哪），之后新发布的放到 {租户}/_skills/ 下。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from atlas_tenancy.migration import add_tenant, drop_tenant
from sqlalchemy.dialects import postgresql

revision: str = "c0005_tenant_market"
down_revision: str | Sequence[str] | None = "c0004_skill_app"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("skill", "skill_version", "skill_audit")
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_TS = sa.DateTime(timezone=True)


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
    op.drop_constraint("pk_skill", "skill", type_="primary")
    op.create_primary_key("pk_skill", "skill", ["tenant_uuid", "slug"])
    op.drop_constraint("uq_skill_version_slug_version", "skill_version", type_="unique")
    op.create_unique_constraint(
        "uq_skill_version_tenant_uuid_slug_version", "skill_version", ["tenant_uuid", "slug", "version"]
    )
    op.drop_constraint(op.f("ck_skill_source_enum"), "skill", type_="check")
    op.create_check_constraint(
        op.f("ck_skill_source_enum"), "skill", "source IN ('builtin','tenant','imported','market')"
    )

    op.create_table(
        "market_skill",
        sa.Column("slug", sa.String(63), primary_key=True),
        sa.Column("install_by_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
    )
    op.create_table(
        "market_skill_version",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
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
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("published_by", sa.String(255), nullable=False),
        sa.Column("published_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.UniqueConstraint("slug", "version", name="uq_market_skill_version_slug_version"),
        sa.CheckConstraint(
            "status IN ('published','revoked')", name=op.f("ck_market_skill_version_status_enum")
        ),
    )


def downgrade() -> None:
    op.drop_table("market_skill_version")
    op.drop_table("market_skill")
    op.drop_constraint(op.f("ck_skill_source_enum"), "skill", type_="check")
    op.create_check_constraint(
        op.f("ck_skill_source_enum"), "skill", "source IN ('builtin','tenant','imported')"
    )
    op.drop_constraint("uq_skill_version_tenant_uuid_slug_version", "skill_version", type_="unique")
    op.create_unique_constraint(
        "uq_skill_version_slug_version", "skill_version", ["slug", "version"]
    )
    op.drop_constraint("pk_skill", "skill", type_="primary")
    op.create_primary_key("pk_skill", "skill", ["slug"])
    for table in reversed(TABLES):
        drop_tenant(op, table)
