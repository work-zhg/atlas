"""审批模式：规划 / 手动 / 自动（doc/native-approval-mode-design.html）

Revision ID: 0022_approval_modes
Revises: 0021_subagent_tree
Create Date: 2026-10-22

thread.approval_mode   用户在会话里选的模式，作用于下一个 run；计划获批时改成用户选的执行模式。
                       NULL = 用智能体的默认模式
run.approval_mode      这个 run 当前的模式：建 run 时取自会话（或请求），计划获批时更新，
                       续跑与接管时从这里读。子 run 记的是派活那一刻父 run 的模式。NULL = 旧数据
approval.kind          tool = 工具调用的确认；plan = 规划模式提交的计划
approval.source        为什么问：manual_mode / auto_uncertain / plan_mode（审计）
approval.reason        给用户看的一句话
approval.outcome       决策带回的附加信息：计划审批选的执行模式、拒绝原因 / 修改意见

★ 不回填：旧的 run 没有模式就按智能体默认处理；作者必审列表（spec.limits.require_approval_for）
  在快照 JSON 里，读取时忽略，不需要迁移。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import JSON_COL_NULLABLE

revision: str = "0022_approval_modes"
down_revision: str | Sequence[str] | None = "0021_subagent_tree"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("thread", sa.Column("approval_mode", sa.String(8), nullable=True))
    op.add_column("run", sa.Column("approval_mode", sa.String(8), nullable=True))
    op.add_column(
        "approval",
        sa.Column("kind", sa.String(8), nullable=False, server_default="tool"),
    )
    op.add_column("approval", sa.Column("source", sa.String(24), nullable=True))
    op.add_column("approval", sa.Column("reason", sa.Text(), nullable=True))
    op.add_column("approval", sa.Column("outcome", JSON_COL_NULLABLE, nullable=True))


def downgrade() -> None:
    op.drop_column("approval", "outcome")
    op.drop_column("approval", "reason")
    op.drop_column("approval", "source")
    op.drop_column("approval", "kind")
    op.drop_column("run", "approval_mode")
    op.drop_column("thread", "approval_mode")
