"""按 C4 目标模型收拾表（c4-model/knowledge/code-*.html）

Revision ID: k0011_c4_target
Revises: k0010_capacity
Create Date: 2026-11-23

    kb_index_version   每个库恰好一个 active 版本（部分唯一索引）；向量模型和 collection 只记在这里
    kb_dataset         去掉 active 版本的副本（embedding_*、collection_name）、接入用户中心前的
                       可见范围（permission）、存量服务账号标记（owner_kind）、没用上的
                       indexing_technique、provider
    kb_dataset_permission   删表（接入用户中心前 partial_members 的成员，早已迁进用户中心）
    kb_segment         去掉 hit_count（命中次数从检索记录汇总）、answer、keywords、tokens
    kb_document        去掉 doc_language、tokens；indexing_status 加索引（入库巡检按它找）

★ 还有存量服务账号的库（owner_kind = service）就不迁：它们没登记到用户中心，删了这一列就没人能用。
  先用上一版的 `sync-uc --service-owner` 改归一个人。
★ 库上的模型和 collection 是 active 版本的指针，以它为准对齐 kb_index_version：同名 collection
  的版本改成 active、其余 active 退役；一个对得上的版本都没有就补一个。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "k0011_c4_target"
down_revision: str | Sequence[str] | None = "k0010_capacity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    service = bind.execute(
        sa.text("SELECT count(*) FROM kb_dataset WHERE owner_kind = 'service'")
    ).scalar()
    if service:
        raise RuntimeError(
            f"还有 {service} 个存量服务账号的库（owner_kind = service）：先用上一版的 "
            "`python -m atlas_knowledge sync-uc --tenant … --service-owner …` 改归一个人再迁移"
        )

    # ① 以库上的指针为准对齐索引版本
    op.execute(
        """
        UPDATE kb_index_version v SET status = 'retired'
        FROM kb_dataset d
        WHERE v.dataset_uuid = d.uuid AND v.status = 'active'
          AND v.collection_name <> d.collection_name
        """
    )
    op.execute(
        """
        UPDATE kb_index_version v SET status = 'active', activated_at = COALESCE(v.activated_at, now())
        FROM kb_dataset d
        WHERE v.dataset_uuid = d.uuid AND v.collection_name = d.collection_name
          AND v.status <> 'active'
          AND NOT EXISTS (
              SELECT 1 FROM kb_index_version a
              WHERE a.dataset_uuid = d.uuid AND a.status = 'active'
          )
        """
    )
    op.execute(
        """
        INSERT INTO kb_index_version
            (uuid, created_at, updated_at, tenant_uuid, dataset_uuid, version, embedding_provider,
             embedding_model, embedding_dim, collection_name, status, auto_activate, activated_at,
             created_by)
        SELECT gen_random_uuid(), now(), now(), d.tenant_uuid, d.uuid,
               COALESCE(
                   (SELECT max(v.version) FROM kb_index_version v WHERE v.dataset_uuid = d.uuid), 0
               ) + 1,
               d.embedding_provider, d.embedding_model, d.embedding_dim, d.collection_name,
               'active', false, d.created_at, d.created_by
        FROM kb_dataset d
        WHERE NOT EXISTS (
            SELECT 1 FROM kb_index_version v WHERE v.dataset_uuid = d.uuid AND v.status = 'active'
        )
        """
    )
    op.create_index(
        "ux_kb_index_version_active",
        "kb_index_version",
        ["dataset_uuid"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # ② 去掉不再用的列和表
    for col in (
        "embedding_provider",
        "embedding_model",
        "embedding_dim",
        "collection_name",
        "permission",
        "owner_kind",
        "indexing_technique",
        "provider",
    ):
        op.drop_column("kb_dataset", col)
    op.drop_table("kb_dataset_permission")
    for col in ("hit_count", "answer", "keywords", "tokens"):
        op.drop_column("kb_segment", col)
    for col in ("doc_language", "tokens"):
        op.drop_column("kb_document", col)
    op.create_index("ix_kb_document_indexing_status", "kb_document", ["indexing_status"])


def downgrade() -> None:
    op.drop_index("ix_kb_document_indexing_status", table_name="kb_document")
    op.add_column("kb_document", sa.Column("doc_language", sa.String(32), nullable=True))
    op.add_column(
        "kb_document", sa.Column("tokens", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("kb_segment", sa.Column("answer", sa.Text(), nullable=True))
    op.add_column("kb_segment", sa.Column("keywords", _JSON, nullable=True))
    op.add_column(
        "kb_segment", sa.Column("tokens", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "kb_segment", sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.create_table(
        "kb_dataset_permission",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("uuid", sa.Uuid(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_uuid", sa.Uuid(), nullable=False),
        sa.Column("dataset_uuid", sa.Uuid(), nullable=False, index=True),
        sa.Column("principal_id", sa.String(64), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.UniqueConstraint("dataset_uuid", "principal_id"),
    )
    for name, type_, default in (
        ("provider", sa.String(16), "internal"),
        ("indexing_technique", sa.String(32), "high_quality"),
        ("owner_kind", sa.String(16), "user"),
        ("permission", sa.String(32), "only_me"),
    ):
        op.add_column("kb_dataset", sa.Column(name, type_, nullable=False, server_default=default))
    op.execute("UPDATE kb_dataset SET permission = 'all_team' WHERE visibility = 'public'")
    for name, type_ in (
        ("embedding_provider", sa.String(64)),
        ("embedding_model", sa.String(255)),
        ("embedding_dim", sa.Integer()),
        ("collection_name", sa.String(128)),
    ):
        op.add_column("kb_dataset", sa.Column(name, type_, nullable=True))
    op.execute(
        """
        UPDATE kb_dataset d SET embedding_provider = v.embedding_provider,
               embedding_model = v.embedding_model, embedding_dim = v.embedding_dim,
               collection_name = v.collection_name
        FROM kb_index_version v
        WHERE v.dataset_uuid = d.uuid AND v.status = 'active'
        """
    )
    for name in ("embedding_provider", "embedding_model", "embedding_dim", "collection_name"):
        op.alter_column("kb_dataset", name, nullable=False)
    op.drop_index("ux_kb_index_version_active", table_name="kb_index_version")
