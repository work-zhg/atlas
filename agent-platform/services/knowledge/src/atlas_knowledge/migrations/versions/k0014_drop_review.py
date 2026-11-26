"""去掉入库审核：删 kb_dataset.review_required 和 kb_document 的审核四列

Revision ID: k0014_drop_review
Revises: k0013_drop_document_acl
Create Date: 2026-11-25

文档可被检索只看启用、归档。存量：
    已驳回（rejected）   改成停用：本来就检索不到，去掉审核后也不该突然出现；库的所有者可以再启用
    待审核（pending）    放开：库里的状态在这里就对了，但向量库里的 doc_ok 还是 false，
                         部署后要跑一次 python -m atlas_knowledge sync-doc-ok
审核人、审核时间、审核意见不保留。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k0014_drop_review"
down_revision: str | Sequence[str] | None = "k0013_drop_document_acl"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("UPDATE kb_document SET enabled = false WHERE review_status = 'rejected'")
    for col in ("review_note", "reviewed_at", "reviewed_by", "review_status"):
        op.drop_column("kb_document", col)
    op.drop_column("kb_dataset", "review_required")


def downgrade() -> None:
    # 审核记录回不来：降级后全部文档都是「不需要审核」，全部库都不要求审核
    op.add_column(
        "kb_dataset",
        sa.Column("review_required", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "kb_document",
        sa.Column("review_status", sa.String(16), nullable=False, server_default="none"),
    )
    op.add_column("kb_document", sa.Column("reviewed_by", sa.String(64), nullable=True))
    op.add_column("kb_document", sa.Column("reviewed_at", _TS, nullable=True))
    op.add_column("kb_document", sa.Column("review_note", sa.Text(), nullable=True))
