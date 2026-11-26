"""去掉文档级权限：删 kb_document.acl

Revision ID: k0013_drop_document_acl
Revises: k0012_enhance_cache
Create Date: 2026-11-25

只留库级：公有的库任何人可读，私有的库按用户中心数据权限（AtlasKnowledgeBase）判，不再按本人、
角色、部门展开的主体标签过滤文档。原来设了 acl 的文档，升级后对能读这个库的人全部可见。

向量库里旧 point 的 payload 还带着 acl 键和它的 payload 索引：检索不再看它，留着无害；
重建索引版本时新 collection 里就没有了。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "k0013_drop_document_acl"
down_revision: str | Sequence[str] | None = "k0012_enhance_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.drop_column("kb_document", "acl")


def downgrade() -> None:
    # 删掉的标签回不来：降级后全部文档都是库内公开
    op.add_column("kb_document", sa.Column("acl", _JSON, nullable=True))
