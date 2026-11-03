"""run 状态去掉 awaiting_approval，新增 awaiting_user（改造计划 A2）

Revision ID: 0030_run_awaiting_user
Revises: 0029_logical_keys
Create Date: 2026-11-03

awaiting_approval 一个状态两种意思：原生智能体的审批挂起（没有进程在跑），和 CLI 智能体在线等
权限响应（进程还在跑）。续跑判据只能靠「有没有等待项」去猜，猜错了就会把在跑的 CLI 再执行一遍
（c4-model/agent/code-executor-segment.html）。改成：

    原生智能体等审批、提问    suspended，在等什么看 waiting_on 的原因
    CLI 在线等权限            running + awaiting_user

存量数据按租约区分：持有租约的是 CLI 在线等待（进程在跑），没有的是原生挂起（挂起时清了租约）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import dialect

revision: str = "0030_run_awaiting_user"
down_revision: str | Sequence[str] | None = "0029_logical_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_STATUSES = (
    "'queued','running','succeeded','failed','cancelled','interrupted','awaiting_approval','suspended'"
)
_NEW_STATUSES = "'queued','running','succeeded','failed','cancelled','interrupted','suspended'"
_OLD_ACTIVE = "'queued','running','awaiting_approval','suspended'"
_NEW_ACTIVE = "'queued','running','suspended'"

#: 约束的真名：0001 建表时传的名字已带 ck_run_ 前缀，命名约定又加了一层（见 0010）
_CK = "ck_run_ck_run_status_enum"


def _swap_check(statuses: str) -> None:
    op.execute(f"ALTER TABLE run DROP CONSTRAINT {_CK}")
    op.execute(f"ALTER TABLE run ADD CONSTRAINT {_CK} CHECK (status IN ({statuses}))")


def _swap_index(statuses: str) -> None:
    if dialect() in ("mysql", "mariadb"):
        return  # MySQL 无分部索引，那边的 ix_run_active 本来就没有 WHERE
    op.execute("DROP INDEX ix_run_active")
    op.execute(f"CREATE INDEX ix_run_active ON run (status) WHERE status IN ({statuses})")


def upgrade() -> None:
    op.add_column(
        "run",
        sa.Column("awaiting_user", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        "UPDATE run SET status = 'running', awaiting_user = TRUE "
        "WHERE status = 'awaiting_approval' AND owner IS NOT NULL"
    )
    op.execute("UPDATE run SET status = 'suspended' WHERE status = 'awaiting_approval'")
    _swap_check(_NEW_STATUSES)
    _swap_index(_NEW_ACTIVE)


def downgrade() -> None:
    _swap_check(_OLD_STATUSES)
    _swap_index(_OLD_ACTIVE)
    op.execute(
        "UPDATE run SET status = 'awaiting_approval' "
        "WHERE status = 'running' AND awaiting_user = TRUE"
    )
    op.drop_column("run", "awaiting_user")
