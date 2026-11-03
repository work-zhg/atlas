"""CLI 会话的接续信息收成一张会话级的表 cli_session（改造计划 A2）

Revision ID: 0031_cli_session
Revises: 0030_run_awaiting_user
Create Date: 2026-11-03

原先一半在 thread 上（external_session_id：CLI 自己的会话 id），一半在 run 上
（acp_bridge_instance / acp_acked_seq：已处理到的 bridge 实例与序号）。序号是会话级的
（跨连接、跨轮连续），放在 run 上又让执行记录带着运行时私有的字段
（c4-model/agent/code-cli-runtime-link.html）。

★ 不搬旧数据：ACP 改名 CLI 时定下「忽略历史数据」，存量 acp 智能体的会话不再能跑，
  接续信息搬过来也用不上。降级同理，旧列重建出来是空的。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import TS_COL, UUID_COL, now_default

revision: str = "0031_cli_session"
down_revision: str | Sequence[str] | None = "0030_run_awaiting_user"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cli_session",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("thread_uuid", UUID_COL, nullable=False),
        sa.Column("agent_session_id", sa.String(128), nullable=True),
        sa.Column("bridge_instance", sa.String(64), nullable=True),
        sa.Column("acked_seq", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", TS_COL, nullable=False, server_default=now_default()),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
        sa.PrimaryKeyConstraint("id", name="pk_cli_session"),
        sa.UniqueConstraint("thread_uuid", name="uk_cli_session_thread"),
    )
    op.drop_column("thread", "external_session_id")
    op.drop_column("run", "acp_bridge_instance")
    op.drop_column("run", "acp_acked_seq")


def downgrade() -> None:
    op.add_column(
        "run",
        sa.Column("acp_acked_seq", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("run", sa.Column("acp_bridge_instance", sa.String(64), nullable=True))
    op.add_column("thread", sa.Column("external_session_id", sa.String(128), nullable=True))
    op.drop_table("cli_session")
