"""去掉「内置智能体」：删除 0002 种下的通用助手，删除 agent.is_builtin

Revision ID: 0024_drop_builtin_agent
Revises: 0023_agent_auth_subject
Create Date: 2026-10-23

不再有内置智能体：所有智能体都由人创建、按用户中心数据权限 AtlasAgent 共享，没有「人人可用、
只有管理员能改、不可删除」的特例（数字员工设计 §8.1）。

★ 通用助手没有会话时物理删除（连同版本）；已经有人用过时不删 —— run 要靠 agent_version
  解释历史 —— 改成普通的已归档智能体，历史会话照常可读。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_drop_builtin_agent"
down_revision: str | Sequence[str] | None = "0023_agent_auth_subject"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    builtin = [
        r[0] for r in conn.execute(sa.text("SELECT id FROM agent WHERE is_builtin")).fetchall()
    ]
    for agent_id in builtin:
        used = conn.execute(
            sa.text("SELECT 1 FROM thread WHERE agent_id = :a LIMIT 1"), {"a": agent_id}
        ).first()
        if used:
            conn.execute(
                sa.text("UPDATE agent SET status = 'archived' WHERE id = :a"), {"a": agent_id}
            )
            continue
        conn.execute(
            sa.text("UPDATE agent SET current_version_id = NULL WHERE id = :a"), {"a": agent_id}
        )
        conn.execute(sa.text("DELETE FROM agent_version WHERE agent_id = :a"), {"a": agent_id})
        conn.execute(sa.text("DELETE FROM agent WHERE id = :a"), {"a": agent_id})
    op.drop_column("agent", "is_builtin")


def downgrade() -> None:
    # 只恢复列；通用助手不再种回来（要的话重跑 0002 的种子）
    op.add_column(
        "agent",
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
