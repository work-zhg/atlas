"""drop_process_requirement：发起流程只填名称

要做什么由执行人在第一个节点的对话里告诉 Agent；不再有固定的「需求」字段。
已有流程的需求文本仍在当时发给 Agent 的开工消息（tf_node_message）里。

Revision ID: t0005
Revises: t0004
Create Date: 2026-10-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "t0005"
down_revision: str | Sequence[str] | None = "t0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("tf_process", "requirement")


def downgrade() -> None:
    op.add_column(
        "tf_process", sa.Column("requirement", sa.Text(), nullable=False, server_default="")
    )
