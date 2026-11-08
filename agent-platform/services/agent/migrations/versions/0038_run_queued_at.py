"""run 跟着请求走，异常归兜底任务（c4-model/agent/executor/design-run-dispatch.html）

Revision ID: 0038_run_queued_at
Revises: 0037_sandbox_provisioning
Create Date: 2026-11-07

run.queued_at：进入排队的时刻。建 run、续跑放回排队时写，被抢到时清掉。兜底任务只碰排队超过
5 秒的：请求那条路从建 run 到本实例抢锁只有几毫秒，5 秒还在排队，才说明本实例没接下。

★ 已经在排队的行补成 created_at：它们是旧版本留下的，早就过了 5 秒，下一轮兜底就接走。
★ ix_run_status_created 只给原来的领取扫描（按创建时间取最老的一条）用，领取扫描去掉了，
  索引一并删掉；兜底按 id 翻页，排队中的走 ix_run_queued，在途的走 ix_run_status_lease。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import TS_COL

revision: str = "0038_run_queued_at"
down_revision: str | Sequence[str] | None = "0037_sandbox_provisioning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run", sa.Column("queued_at", TS_COL, nullable=True))
    op.execute("UPDATE run SET queued_at = created_at WHERE status = 'queued'")
    op.create_index("ix_run_queued", "run", ["status", "queued_at"])
    op.drop_index("ix_run_status_created", table_name="run")


def downgrade() -> None:
    op.create_index("ix_run_status_created", "run", ["status", "created_at"])
    op.drop_index("ix_run_queued", table_name="run")
    op.drop_column("run", "queued_at")
