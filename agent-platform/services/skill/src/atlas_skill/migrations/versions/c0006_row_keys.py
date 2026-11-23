"""表按约定改成自增 id + uuid 逻辑主键；版本用 skill_uuid 引用技能；待审查用部分唯一索引兜底

Revision ID: c0006_row_keys
Revises: c0005_tenant_market
Create Date: 2026-11-23

c4-model/skill/code-versions.html「表按约定加自增 id 和 uuid，对外仍用 slug + 版本号」。

    skill                  主键 (tenant_uuid, slug) → id；加 uuid、updated_at；(tenant_uuid, slug) 唯一
    skill_version          原来的 id（上传 id）改名 uuid，新加自增 id；slug → skill_uuid；
                           (skill_uuid, version) 唯一；一个技能只能有一个待审查（部分唯一索引）
    skill_audit            原来的 id 改名 uuid，新加自增 id；at → created_at，加 updated_at
    market_skill           主键 slug → id；加 uuid、updated_at；slug 唯一
    market_skill_version   原来的 id 改名 uuid，新加自增 id；slug → market_skill_uuid；加 created_at

接口、对象存储路径不变：上传 id 还是原来那个值，slug + 版本号照旧。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from atlas_skill.db.types import uuid7

revision: str = "c0006_row_keys"
down_revision: str | Sequence[str] | None = "c0005_tenant_market"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _pk_name(table: str) -> str:
    """skill_audit 的主键还叫 pk_config_audit（c0004 由 config_audit 改名而来）。"""
    name = sa.inspect(op.get_bind()).get_pk_constraint(table)["name"]
    assert name, f"{table} 没有主键"
    return str(name)


def _add_row_keys(table: str, *, rename_id: bool) -> None:
    """自增 id 当主键，uuid 是逻辑主键。rename_id：原来的 uuid 主键改名 uuid，值不变。"""
    op.drop_constraint(_pk_name(table), table, type_="primary")
    if rename_id:
        op.alter_column(table, "id", new_column_name="uuid")
    else:
        op.add_column(table, sa.Column("uuid", sa.Uuid(), nullable=True))
        bind = op.get_bind()
        keys = list(bind.execute(sa.text(f"SELECT ctid::text FROM {table}")).scalars())
        for key in keys:
            bind.execute(
                sa.text(f"UPDATE {table} SET uuid = :u WHERE ctid::text = :k"),
                {"u": uuid7(), "k": key},
            )
        op.alter_column(table, "uuid", nullable=False)
    op.execute(f"ALTER TABLE {table} ADD COLUMN id BIGSERIAL")
    op.create_primary_key(f"pk_{table}", table, ["id"])
    op.create_unique_constraint(f"uq_{table}_uuid", table, ["uuid"])


def _add_updated_at(table: str, source: str) -> None:
    op.add_column(table, sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(f"UPDATE {table} SET updated_at = {source}")
    op.alter_column(table, "updated_at", nullable=False)


def upgrade() -> None:
    # ── skill
    _add_row_keys("skill", rename_id=False)
    _add_updated_at("skill", "created_at")
    op.create_unique_constraint("uq_skill_tenant_uuid_slug", "skill", ["tenant_uuid", "slug"])

    # ── skill_version：slug → skill_uuid
    _add_row_keys("skill_version", rename_id=True)
    op.add_column("skill_version", sa.Column("skill_uuid", sa.Uuid(), nullable=True))
    op.execute(
        "UPDATE skill_version v SET skill_uuid = s.uuid FROM skill s"
        " WHERE s.tenant_uuid = v.tenant_uuid AND s.slug = v.slug"
    )
    op.alter_column("skill_version", "skill_uuid", nullable=False)
    op.drop_constraint("uq_skill_version_tenant_uuid_slug_version", "skill_version", type_="unique")
    op.drop_index("ix_skill_version_slug_status", table_name="skill_version")
    op.drop_column("skill_version", "slug")
    op.create_unique_constraint(
        "uq_skill_version_skill_uuid_version", "skill_version", ["skill_uuid", "version"]
    )
    op.create_index(
        "uq_skill_version_pending",
        "skill_version",
        ["skill_uuid"],
        unique=True,
        postgresql_where=sa.text("status = 'pending_review'"),
    )
    op.create_index("ix_skill_version_skill_uuid_status", "skill_version", ["skill_uuid", "status"])

    # ── skill_audit：at → created_at（ix_skill_audit_target 跟着列改名）
    _add_row_keys("skill_audit", rename_id=True)
    op.alter_column("skill_audit", "at", new_column_name="created_at")
    _add_updated_at("skill_audit", "created_at")

    # ── market_skill
    _add_row_keys("market_skill", rename_id=False)
    _add_updated_at("market_skill", "created_at")
    op.create_unique_constraint("uq_market_skill_slug", "market_skill", ["slug"])

    # ── market_skill_version：slug → market_skill_uuid
    _add_row_keys("market_skill_version", rename_id=True)
    op.add_column(
        "market_skill_version", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("UPDATE market_skill_version SET created_at = published_at")
    op.alter_column("market_skill_version", "created_at", nullable=False)
    op.add_column("market_skill_version", sa.Column("market_skill_uuid", sa.Uuid(), nullable=True))
    op.execute(
        "UPDATE market_skill_version v SET market_skill_uuid = s.uuid FROM market_skill s"
        " WHERE s.slug = v.slug"
    )
    op.alter_column("market_skill_version", "market_skill_uuid", nullable=False)
    op.drop_constraint(
        "uq_market_skill_version_slug_version", "market_skill_version", type_="unique"
    )
    op.drop_column("market_skill_version", "slug")
    op.create_unique_constraint(
        "uq_market_skill_version_market_skill_uuid_version",
        "market_skill_version",
        ["market_skill_uuid", "version"],
    )


def _drop_row_keys(table: str, *, rename_back: bool) -> None:
    op.drop_constraint(f"uq_{table}_uuid", table, type_="unique")
    op.drop_constraint(f"pk_{table}", table, type_="primary")
    op.drop_column(table, "id")
    if rename_back:
        op.alter_column(table, "uuid", new_column_name="id")
    else:
        op.drop_column(table, "uuid")


def downgrade() -> None:
    # ── market_skill_version
    op.add_column("market_skill_version", sa.Column("slug", sa.String(63), nullable=True))
    op.execute(
        "UPDATE market_skill_version v SET slug = s.slug FROM market_skill s"
        " WHERE s.uuid = v.market_skill_uuid"
    )
    op.alter_column("market_skill_version", "slug", nullable=False)
    op.drop_constraint(
        "uq_market_skill_version_market_skill_uuid_version", "market_skill_version", type_="unique"
    )
    op.drop_column("market_skill_version", "market_skill_uuid")
    op.drop_column("market_skill_version", "created_at")
    _drop_row_keys("market_skill_version", rename_back=True)
    op.create_primary_key("pk_market_skill_version", "market_skill_version", ["id"])
    op.create_unique_constraint(
        "uq_market_skill_version_slug_version", "market_skill_version", ["slug", "version"]
    )

    # ── market_skill
    op.drop_constraint("uq_market_skill_slug", "market_skill", type_="unique")
    op.drop_column("market_skill", "updated_at")
    _drop_row_keys("market_skill", rename_back=False)
    op.create_primary_key("pk_market_skill", "market_skill", ["slug"])

    # ── skill_audit
    op.drop_column("skill_audit", "updated_at")
    op.alter_column("skill_audit", "created_at", new_column_name="at")
    _drop_row_keys("skill_audit", rename_back=True)
    op.create_primary_key("pk_config_audit", "skill_audit", ["id"])

    # ── skill_version
    op.drop_index("ix_skill_version_skill_uuid_status", table_name="skill_version")
    op.drop_index("uq_skill_version_pending", table_name="skill_version")
    op.drop_constraint("uq_skill_version_skill_uuid_version", "skill_version", type_="unique")
    op.add_column("skill_version", sa.Column("slug", sa.String(63), nullable=True))
    op.execute("UPDATE skill_version v SET slug = s.slug FROM skill s WHERE s.uuid = v.skill_uuid")
    op.alter_column("skill_version", "slug", nullable=False)
    op.drop_column("skill_version", "skill_uuid")
    _drop_row_keys("skill_version", rename_back=True)
    op.create_primary_key("pk_skill_version", "skill_version", ["id"])
    op.create_unique_constraint(
        "uq_skill_version_tenant_uuid_slug_version",
        "skill_version",
        ["tenant_uuid", "slug", "version"],
    )
    op.create_index("ix_skill_version_slug_status", "skill_version", ["slug", "status"])

    # ── skill
    op.drop_constraint("uq_skill_tenant_uuid_slug", "skill", type_="unique")
    op.drop_column("skill", "updated_at")
    _drop_row_keys("skill", rename_back=False)
    op.create_primary_key("pk_skill", "skill", ["tenant_uuid", "slug"])
