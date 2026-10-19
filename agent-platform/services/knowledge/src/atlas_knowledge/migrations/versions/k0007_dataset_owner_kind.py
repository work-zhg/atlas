"""库级权限接入用户中心（数据编码 AtlasKnowledgeBase）

Revision ID: k0007_dataset_owner_kind
Revises: k0006_segment_meta
Create Date: 2026-10-16

kb_dataset.owner_kind：user = 用户中心的人建的（权限全在用户中心）；service = 服务账号建的
（只有它自己能用）。存量一律视为 user —— 由 `python -m atlas_knowledge sync-uc` 把旧的可见范围
与成员写进用户中心，所有者在用户中心里不存在的（服务账号的库）改为 service。

kb_dataset.permission 与 kb_dataset_permission 暂留（只读），sync-uc 跑完后的下一个版本删除。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0007_dataset_owner_kind"
down_revision: str | Sequence[str] | None = "k0006_segment_meta"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "kb_dataset",
        sa.Column("owner_kind", sa.String(16), nullable=False, server_default="user"),
    )


def downgrade() -> None:
    op.drop_column("kb_dataset", "owner_kind")
