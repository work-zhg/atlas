"""SubAgent 可以继续派 SubAgent：会话树与 run 树记下根和深度（doc/detail/subagent.html）

Revision ID: 0021_subagent_tree
Revises: 0020_identity_headers
Create Date: 2026-10-19

thread.root_thread_id   会话树的根。NULL = 自己就是根。工作区与事件流都归根会话 ——
                        原先由 `parent_thread_id or id` 派生，只往上看一层，深度封顶 1 时成立
run.root_run_id         run 树的根（一轮用户输入的那个 run）。NULL = 自己就是根。
                        SubAgent 的累计上限按整棵树数（subagent_max_per_run），防递归跑飞
run.depth               在 run 树里的深度：根 0、子 1、孙 2 …… 进 TraceEvent.depth

★ 回填按「深度封顶 1」的旧数据：子会话的根就是它的父会话，子 run 的根就是它的父 run。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import UUID_COL

revision: str = "0021_subagent_tree"
down_revision: str | Sequence[str] | None = "0020_identity_headers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("thread", sa.Column("root_thread_id", UUID_COL, nullable=True))
    op.add_column("run", sa.Column("root_run_id", UUID_COL, nullable=True))
    op.add_column(
        "run", sa.Column("depth", sa.Integer(), nullable=False, server_default=sa.text("0"))
    )
    # 准入时按整棵树数子 run
    op.create_index("ix_run_root", "run", ["root_run_id"])

    op.execute(
        "UPDATE thread SET root_thread_id = parent_thread_id WHERE parent_thread_id IS NOT NULL"
    )
    op.execute(
        "UPDATE run SET root_run_id = parent_run_id, depth = 1 WHERE parent_run_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_run_root", table_name="run")
    op.drop_column("run", "depth")
    op.drop_column("run", "root_run_id")
    op.drop_column("thread", "root_thread_id")
