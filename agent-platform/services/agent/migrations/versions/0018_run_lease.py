"""run 的租约与断点续跑（doc/horizontal-scaling-design.html §4 · §5 · §11）

Revision ID: 0018_run_lease
Revises: 0017_thread_context_state
Create Date: 2026-10-15

run.owner / lease_until / claimed_at / takeovers：执行器领取 run 的租约（CAS 领取、续租、过期接管）。
run.acp_bridge_instance / acp_acked_seq：acp run 已确认到的 bridge 序号，换实例接管时据此 attach。

★ 已有的 running 行 lease_until 为空，不会被当成「过期」接管 —— 它们是旧版本进程留下的，
  不是本机制下的在途 run，自动续跑一个可能几周前的 run 不是任何人想要的。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import TS_COL

revision: str = "0018_run_lease"
down_revision: str | Sequence[str] | None = "0017_thread_context_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run", sa.Column("owner", sa.String(64), nullable=True))
    op.add_column("run", sa.Column("lease_until", TS_COL, nullable=True))
    op.add_column("run", sa.Column("claimed_at", TS_COL, nullable=True))
    op.add_column(
        "run", sa.Column("takeovers", sa.Integer(), nullable=False, server_default=sa.text("0"))
    )
    op.add_column("run", sa.Column("acp_bridge_instance", sa.String(64), nullable=True))
    op.add_column(
        "run",
        sa.Column("acp_acked_seq", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    # 领取的两条路：排队的按创建时间、在途的按租约到期
    op.create_index("ix_run_status_created", "run", ["status", "created_at"])
    op.create_index("ix_run_status_lease", "run", ["status", "lease_until"])


def downgrade() -> None:
    op.drop_index("ix_run_status_lease", table_name="run")
    op.drop_index("ix_run_status_created", table_name="run")
    for column in (
        "acp_acked_seq",
        "acp_bridge_instance",
        "takeovers",
        "claimed_at",
        "lease_until",
        "owner",
    ):
        op.drop_column("run", column)
