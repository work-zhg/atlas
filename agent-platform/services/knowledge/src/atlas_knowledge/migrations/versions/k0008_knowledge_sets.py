"""知识集：知识库编成组，智能体按知识集使用（doc/design/knowledge-set-design.html）

Revision ID: k0008_knowledge_sets
Revises: k0007_dataset_owner_kind
Create Date: 2026-11-12

- kb_knowledge_set：名称、介绍、图标、维护人、每次带回几段
- kb_dataset.set_uuid：所属知识集；kb_dataset.visibility：public / private
- 存量：每个库 1:1 建一个同名知识集，**知识集的 uuid 就用库的 uuid** —— 智能体旧配置里引用的
  库 id 原样就是知识集 id（Atlas 读旧快照时不用查表）。介绍沿用库的介绍，维护人 = 库的所有者。
  原来「全员可见」（permission = all_team）的库标为公有，其余私有（照旧按用户中心授权）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "k0008_knowledge_sets"
down_revision: str | Sequence[str] | None = "k0007_dataset_owner_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "kb_knowledge_set",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("uuid", sa.Uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("icon", sa.String(64), nullable=False, server_default="📁"),
        sa.Column("maintainers", _JSON, nullable=False),
        sa.Column("top_k", sa.Integer, nullable=False, server_default="4"),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_kb_knowledge_set"),
        sa.UniqueConstraint("uuid", name="uq_kb_knowledge_set_uuid"),
    )
    op.add_column("kb_dataset", sa.Column("set_uuid", sa.Uuid, nullable=True))
    op.add_column(
        "kb_dataset",
        sa.Column("visibility", sa.String(16), nullable=False, server_default="private"),
    )
    op.execute(
        """
        INSERT INTO kb_knowledge_set
            (uuid, created_at, updated_at, name, description, icon, maintainers, top_k, created_by)
        SELECT uuid, created_at, now(), name, description, icon, jsonb_build_array(owner_id), 4,
               created_by
        FROM kb_dataset
        """
    )
    op.execute("UPDATE kb_dataset SET set_uuid = uuid")
    op.execute("UPDATE kb_dataset SET visibility = 'public' WHERE permission = 'all_team'")
    op.alter_column("kb_dataset", "set_uuid", nullable=False)
    op.create_index("ix_kb_dataset_set_uuid", "kb_dataset", ["set_uuid"])


def downgrade() -> None:
    op.drop_index("ix_kb_dataset_set_uuid", table_name="kb_dataset")
    op.drop_column("kb_dataset", "visibility")
    op.drop_column("kb_dataset", "set_uuid")
    op.drop_table("kb_knowledge_set")
