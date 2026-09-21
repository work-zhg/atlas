"""会话摘要持久化：压缩只付一次钱

Revision ID: 0005_thread_summary
Revises: 0004_dry_run
Create Date: 2026-08-20

§7.4 原本设想「压缩改写 LangGraph checkpoint 并持久化」。但本项目**没有
checkpointer** —— 每个 run 的历史都是从 `message` 表重建的（见
InProcessExecutor._prepare）。照搬那句话就变成「每个 run 都重新压一次」，
而 §7.4 明确警告过这一点：摘要的 token 每轮重付，且 prompt cache 每轮击穿。

等价做法：把摘要连同覆盖边界存在 thread 上。
  · summary            摘要正文
  · summary_upto       摘要覆盖到哪条消息（含）的时间戳
  · compact_count      已有列，累加用于监控与 UI 提示

下一个 run 的历史 = [摘要] + summary_upto 之后的消息。

★ `message` 表一个字节都不动 —— §7.4 的两套存储：模型视角被压缩，
  用户视角完整保留，UI 滚到顶仍能看到第一条消息。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_server.db.migration_types import TS_COL

revision: str = "0005_thread_summary"
down_revision: str | Sequence[str] | None = "0004_dry_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("thread", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column(
        "thread",
        sa.Column("summary_upto", TS_COL, nullable=True),
    )
    # 摘要要么两者都有、要么都没有：只有其一时无从判断该保留哪些消息
    op.create_check_constraint(
        "thread_summary_pair",
        "thread",
        "(summary IS NULL) = (summary_upto IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("thread_summary_pair", "thread", type_="check")
    op.drop_column("thread", "summary_upto")
    op.drop_column("thread", "summary")
